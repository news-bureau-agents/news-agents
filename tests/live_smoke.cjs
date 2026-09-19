/* Opt-in live smoke: creates and deletes ONE temporary Supabase Auth user.
 * Requires SUPABASE_PROJECT_REF, PLAYWRIGHT_MODULE, CHROME_EXECUTABLE.
 * Credentials stay in process memory; no session/profile screenshots or traces.
 */
const { execFileSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const assert = require('node:assert/strict');
const { mkdirSync } = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const origin = process.env.OASIS_URL || 'https://oasis.carlunpen.com';
  const ref = process.env.SUPABASE_PROJECT_REF;
  assert(ref, 'SUPABASE_PROJECT_REF required');
  const keys = JSON.parse(execFileSync('supabase', ['projects', 'api-keys', '--project-ref', ref, '-o', 'json'], {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}));
  const adminKey = keys.find(k => k.name === 'service_role')?.api_key;
  assert(adminKey, 'Administrative key unavailable for ephemeral test user');
  const config = await (await fetch(`${origin}/api/config`)).json();
  assert(config.supabaseUrl === `https://${ref}.supabase.co`, 'Unexpected Supabase project');
  const adminHeaders = {apikey: adminKey, Authorization: `Bearer ${adminKey}`, 'Content-Type': 'application/json'};
  const email = `oasis-smoke-${randomUUID()}@example.com`;
  const password = `${randomUUID()}Aa9!`;
  let userId;
  let browser;
  try {
    for (const path of ['/', '/healthz', '/readyz']) {
      const response = await fetch(origin + path);
      assert(response.status === 200, `${path} not healthy`);
      assert(response.headers.get('strict-transport-security'), 'HSTS missing');
      assert(response.headers.get('cache-control').includes('no-store'));
    }
    const redirect = await fetch(origin.replace('https:', 'http:'), {redirect: 'manual'});
    assert([301, 308].includes(redirect.status), 'HTTP must redirect to HTTPS');
    for (const headers of [{}, {Authorization: 'Bearer invalid-token'}]) {
      assert((await fetch(`${origin}/api/me`, {headers})).status === 401, 'Unauthorized request accepted');
    }
    const created = await fetch(`${config.supabaseUrl}/auth/v1/admin/users`, {
      method: 'POST', headers: adminHeaders,
      body: JSON.stringify({email, password, email_confirm: true})
    });
    assert(created.ok, `Temporary user creation failed: HTTP ${created.status}`);
    userId = (await created.json()).id;
    assert(userId, 'Temporary user id missing');
    browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_EXECUTABLE});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.name));
    await page.goto(origin);
    await page.locator('#email').fill(email);
    await page.locator('#password').fill('intentionally-wrong-password');
    await page.locator('#login-button').click();
    await page.locator('#login-status').filter({hasText: 'Sign-in failed'}).waitFor();
    assert(await page.locator('#password').inputValue() === '', 'Password was not cleared');
    await page.locator('#password').fill(password);
    await page.locator('#login-button').click();
    await page.waitForFunction(() => document.querySelector('#profile-output').textContent.includes('"id"'));
    const profile = JSON.parse(await page.locator('#profile-output').textContent());
    assert(profile.id === userId, 'Wrong authenticated profile');
    assert(await page.evaluate(() => localStorage.length === 0 && sessionStorage.length === 0), 'Session persisted in storage');
    assert((await page.context().cookies()).length === 0, 'Unexpected auth cookie');
    await page.reload();
    await page.locator('#login-view').waitFor({state: 'visible'});
    assert(await page.locator('#profile-output').textContent() === '', 'Profile survived reload');
    await page.locator('#email').fill(email);
    await page.locator('#password').fill(password);
    await page.locator('#login-button').click();
    await page.waitForFunction(() => document.querySelector('#profile-output').textContent.includes('"id"'));
    await page.locator('#logout-button').click();
    assert(await page.locator('#profile-output').textContent() === '', 'Profile survived logout');
    await page.reload();
    mkdirSync('artifacts', {recursive: true});
    await page.screenshot({path: 'artifacts/oasis-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile horizontal overflow');
    await page.screenshot({path: 'artifacts/oasis-mobile.png', fullPage: true});
    assert(pageErrors.length === 0, 'Browser runtime errors');
    console.log('PASS: HTTPS, HSTS, HTTP redirect, readiness, unauthorized/invalid JWT rejection, wrong-password handling, real browser sign-in, protected profile, memory-only session, reload, logout, desktop/mobile screenshots.');
  } finally {
    if (browser) await browser.close();
    if (userId) {
      const removed = await fetch(`${config.supabaseUrl}/auth/v1/admin/users/${encodeURIComponent(userId)}`, {method: 'DELETE', headers: adminHeaders});
      if (!removed.ok) throw new Error('Temporary user cleanup failed; manual cleanup required');
      console.log('Temporary test user deleted; credentials were not stored.');
    }
  }
})().catch(error => {console.error(error.message); process.exitCode = 1;});
