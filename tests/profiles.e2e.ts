import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

test('changing Startup URL preserves a multiline inline note', async ({ app, browser }) => {
  const note = 'Line one\nLine two';
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await browser.locator('[aria-label="Edit Notes: Studio Gmail"]').click();
  await browser.locator('#field-value').fill(note);
  await browser.locator('#modal-root .btn-act').click();
  await expect(browser.locator('.row.body:has([aria-label="Edit Notes: Studio Gmail"]) .notes-line')).toHaveAttribute('title', note);
  await browser.locator('[aria-label="Edit: Studio Gmail"]').click();
  await browser.locator('#f-start-url').fill('https://example.test/start');
  await browser.locator('#modal-root .btn-act').click();
  await expect(browser.locator('[role=dialog]')).toHaveCount(0);
  await browser.reload();
  await browser.locator('[aria-label="Edit Notes: Studio Gmail"]').click();
  await expect(browser.locator('#field-value')).toHaveValue(note);
});

test('Space keeps keyboard focus on the selected row', async ({ app, browser }) => {
  await browser.setViewport({ width: 1400, height: 1000 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  const header = browser.locator('.head-row input[type=checkbox]');
  const checkbox = browser.locator('.row.body input[type=checkbox]').first();
  await expect(header).toBeVisible();
  await browser.evaluate(() => document.querySelector<HTMLInputElement>('.head-row input[type=checkbox]')!.focus());
  await browser.keyboard.press('Tab');
  await expect(browser.locator('.row.body input[type=checkbox]:focus')).toHaveCount(1);
  await browser.keyboard.press('Space');
  await expect(checkbox).toBeChecked();
  await expect(browser.locator('.row.body input[type=checkbox]:focus')).toHaveCount(1);
  await browser.keyboard.press('Tab');
  await expect(browser.locator('.row.body .c-id button:focus')).toHaveCount(1);
});

test('profile field editors persist only their fields', async ({ app, browser }) => {
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();
  await browser.locator('[aria-label="Edit ID: Studio Gmail"]').click();
  await browser.locator('#field-value').fill('2');
  await browser.locator('#modal-root .btn-act').click();
  await expect(browser.locator('.row.body').last()).toContainText('Studio Gmail');
  await browser.locator('[aria-label="Edit Profile: Studio Gmail"]').click();
  await browser.locator('#field-value').fill('Studio renamed');
  await browser.locator('#modal-root .btn-act').click();
  const renamed = () => browser.locator('.row.body').filter({ hasText: 'Studio renamed' });
  await expect(renamed()).toContainText('studio@example.test');
  await browser.locator('[aria-label="Edit Tags: Studio renamed"]').click();
  await browser.locator('#tag-input').fill('QA');
  await browser.locator('#tag-input').press('Enter');
  await browser.locator('#tag-input').fill('Antigravity');
  await browser.locator('#tag-input').press('Enter');
  await browser.locator('#modal-root .btn-act').click();
  // All 4 tags must be visible directly on the row without "+1 / +2" hiding
  await expect(renamed()).toContainText('Gmail');
  await expect(renamed()).toContainText('Gemini Pixel');
  await expect(renamed()).toContainText('QA');
  await expect(renamed()).toContainText('Antigravity');
  await browser.locator('[aria-label="Edit Tags: Studio renamed"]').click();
  await expect(browser.locator('[aria-label="Remove QA"]')).toBeVisible();
  await browser.locator('#modal-root .btn-line').last().click();
  // Backup Codes editor (synthetic fixture sheet + on-demand read + generic toast without leaking code)
  await browser.locator('[aria-label="Backup Codes: Studio renamed"]').click();
  await expect(browser.locator('#field-value')).toBeEnabled();
  await browser.locator('#field-value').fill('FIXTURE BACKUP CODES\n1. 1111 0001   6. 1111 0006\n2. 1111 0002   7. 1111 0007');
  await expect(browser.locator('[data-backup-code="1111 0001"]')).toBeVisible();
  await expect(browser.locator('[data-backup-code="1111 0006"]')).toBeVisible();
  await browser.locator('[data-backup-code="1111 0001"]').click();
  await expect(browser.locator('#toast')).toContainText('Backup codes copied');
  await expect(browser.locator('#toast')).not.toContainText('1111 0001');
  await browser.locator('#modal-root .btn-act').click();
  await expect(browser.locator('[role=dialog]')).toHaveCount(0);
  // Notes-only edit via full dialog must preserve saved backup codes when not revealed/changed
  await browser.locator('[aria-label="Edit: Studio renamed"]').click();
  await browser.locator('#f-notes').fill('Preserve backup check');
  await browser.locator('#modal-root .btn-act').click();
  await expect(browser.locator('[role=dialog]')).toHaveCount(0);
  await browser.locator('[aria-label="Backup Codes: Studio renamed"]').click();
  await expect(browser.locator('#field-value')).toHaveValue(/1111 0001/);
  await browser.locator('#modal-root .btn-line').last().click();
  // Rows-per-page selector (5, 10, 20, 50, 100, 200) & horizontal scroll container
  await expect(browser.locator('#sheet-scroll')).toBeVisible();
  await expect(browser.locator('#page-size-select')).toHaveValue('20');
  await expect(browser.locator('#total-count')).toContainText('Total: 2');
  await browser.locator('[aria-label="Edit Proxy: Studio renamed"]').click();
  await browser.locator('#field-value').fill('not valid');
  await browser.locator('#modal-root .btn-act').click();
  await expect(browser.locator('#modal-root .msg.err')).toHaveText(/.+/);
  await browser.locator('#field-value').fill('socks5://127.0.0.1:1080');
  await browser.locator('#modal-root .btn-act').click();
  await expect(renamed()).toContainText('127.0.0.1:1080');
  await browser.locator('[aria-label="Edit Notes: Studio renamed"]').click();
  await browser.locator('#field-value').fill('New reminder');
  await browser.locator('#modal-root .btn-act').click();
  await expect(renamed()).toContainText('New reminder');
  await browser.locator('[aria-label="Copy Gmail: Studio renamed"]').click();
  await expect(browser.locator('#toast')).toContainText('Gmail copied');
  await browser.reload();
  await expect(browser.locator('.row.body').last()).toContainText('Studio renamed');
  await expect(renamed()).toContainText('studio@example.test');
  await expect(renamed()).toContainText('New reminder');
});

test('sidebar tag filters clear stale search and keep rail footer reachable', async ({ app, browser }) => {
  await browser.setViewport({ width: 1400, height: 720 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();
  await expect(browser.locator('#total-count')).toContainText('Total: 2');

  // Narrow with search, then click an unrelated sidebar tag filter: stale search must clear.
  await browser.locator('#search').fill('Studio');
  await expect(browser.locator('#total-count')).toContainText('Total: 1');
  await expect(browser.locator('.row.body')).toHaveCount(1);

  const agyBtn = browser.locator('#filters button').filter({ hasText: 'AGY' });
  await agyBtn.click();
  await expect(browser.locator('#search')).toHaveValue('');
  await expect(agyBtn).toHaveAttribute('aria-pressed', 'true');
  await expect(browser.locator('#total-count')).toContainText('Total: 1');
  await expect(browser.locator('.row.body').first()).toContainText('Research');

  await browser.locator('#filters button').filter({ hasText: 'All' }).click();
  await expect(browser.locator('#total-count')).toContainText('Total: 2');

  // Verify rail nav is independently scrollable with scrollbar gutter and footer stays inside viewport.
  const layoutMetrics = await browser.evaluate(() => {
    const nav = document.getElementById('filters')!;
    const foot = document.querySelector('.rail-foot')!;
    const navStyle = getComputedStyle(nav);
    const navRect = nav.getBoundingClientRect();
    const footRect = foot.getBoundingClientRect();
    const firstCount = nav.querySelector('button .count')!.getBoundingClientRect();
    return {
      overflowY: navStyle.overflowY,
      scrollbarGutter: navStyle.scrollbarGutter,
      footBottom: footRect.bottom,
      viewportHeight: window.innerHeight,
      countRightClearance: navRect.right - firstCount.right,
    };
  });
  expect(layoutMetrics.overflowY).toBe('auto');
  expect(layoutMetrics.scrollbarGutter).toContain('stable');
  expect(layoutMetrics.footBottom).toBeLessThanOrEqual(layoutMetrics.viewportHeight);
  expect(layoutMetrics.countRightClearance).toBeGreaterThanOrEqual(8);
});

test('overflowing Web rail stays reachable at normal, short, and mobile viewports', async ({ app, browser }) => {
  const token = 'fixture-token-' + 't'.repeat(32);
  const longTag = 'LongMobileTag-' + 'x'.repeat(50);
  const createdNames: string[] = [];

  await browser.setViewport({ width: 1400, height: 720 });
  await app.open('/');
  await browser.locator('#token').fill(token);
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  try {
    // Seed 18 synthetic profiles with 18 unique tags (21 total tags) to force genuine nav overflow.
    await browser.evaluate(async ({ tok, longTagName }) => {
      for (let i = 1; i <= 18; i++) {
        const tag = i === 18 ? longTagName : `Overflow Tag ${String(i).padStart(2, '0')}`;
        const res = await fetch('/api/v1/profiles', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tok}` },
          body: JSON.stringify({
            name: `Overflow-${String(i).padStart(2, '0')}`,
            os_type: 'macos',
            tags: [tag],
          }),
        });
        if (!res.ok) throw new Error(`Seed failed (${res.status}) for profile ${i}`);
      }
    }, { tok: token, longTagName: longTag });
    for (let i = 1; i <= 18; i++) createdNames.push(`Overflow-${String(i).padStart(2, '0')}`);

    await browser.reload();
    await expect(browser.locator('#app')).toBeVisible();
    await expect(browser.locator('#filters button').filter({ hasText: 'Overflow Tag 17' })).toHaveCount(1);
    expect(await browser.locator('#filters button').count()).toBeGreaterThanOrEqual(23);

    // 1. Desktop normal height (1400x720): nav overflows independently while footer stays in view.
    const normalMetrics = await browser.evaluate(() => {
      const nav = document.getElementById('filters')!;
      nav.scrollTop = nav.scrollHeight;
      const foot = document.querySelector('.rail-foot')!.getBoundingClientRect();
      const logout = document.getElementById('logout-btn')!.getBoundingClientRect();
      return {
        scrollHeight: nav.scrollHeight,
        clientHeight: nav.clientHeight,
        footBottom: foot.bottom,
        logoutBottom: logout.bottom,
        viewportHeight: window.innerHeight,
      };
    });
    expect(normalMetrics.scrollHeight).toBeGreaterThan(normalMetrics.clientHeight + 100);
    expect(normalMetrics.footBottom).toBeLessThanOrEqual(normalMetrics.viewportHeight);
    expect(normalMetrics.logoutBottom).toBeLessThanOrEqual(normalMetrics.viewportHeight);

    // Toolbar Running toggle preserves search while sidebar tag click clears search.
    await browser.locator('#search').fill('Studio');
    await browser.locator('#running-filter-btn').click();
    await expect(browser.locator('#search')).toHaveValue('Studio');
    await browser.locator('#running-filter-btn').click();
    await expect(browser.locator('#search')).toHaveValue('Studio');

    const lowTagBtn = browser.locator('#filters button').filter({ hasText: 'Overflow Tag 17' });
    await lowTagBtn.click();
    await expect(browser.locator('#search')).toHaveValue('');
    await expect(lowTagBtn).toHaveAttribute('aria-pressed', 'true');
    await expect(browser.locator('.row.body')).toHaveCount(1);
    await expect(browser.locator('.row.body').first()).toContainText('Overflow-17');

    // 2. Short wide desktop viewport (1024x350): rail scrolls as a unit; tags & Logout stay reachable.
    await browser.setViewport({ width: 1024, height: 350 });
    const shortNavHeight = await browser.evaluate(
      () => document.getElementById('filters')!.getBoundingClientRect().height,
    );
    expect(shortNavHeight).toBeGreaterThan(100);

    await browser.evaluate(() => {
      const btn = Array.from(document.querySelectorAll('#filters button')).find((b) =>
        (b.textContent || '').includes('Overflow Tag 16'),
      );
      btn?.scrollIntoView({ block: 'center' });
    });
    const tag16Btn = browser.locator('#filters button').filter({ hasText: 'Overflow Tag 16' });
    await tag16Btn.click();
    await expect(tag16Btn).toHaveAttribute('aria-pressed', 'true');
    await expect(browser.locator('.row.body').first()).toContainText('Overflow-16');

    const logoutCheck = await browser.evaluate(() => {
      const btn = document.getElementById('logout-btn')!;
      btn.scrollIntoView({ block: 'center' });
      const r = btn.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        top: r.top,
        bottom: r.bottom,
        height: r.height,
        vh: window.innerHeight,
        hitIsLogout: Boolean(hit && (hit === btn || btn.contains(hit))),
      };
    });
    expect(logoutCheck.height).toBeGreaterThan(20);
    expect(logoutCheck.top).toBeGreaterThanOrEqual(0);
    expect(logoutCheck.bottom).toBeLessThanOrEqual(logoutCheck.vh + 1);
    expect(logoutCheck.hitIsLogout).toBe(true);

    // 3. Mobile viewport (390x760): valid 64-char tag chip caps width and keeps badge on same chip.
    await browser.setViewport({ width: 390, height: 760 });
    const mobileMetrics = await browser.evaluate((longTagName) => {
      const nav = document.getElementById('filters')!;
      const btn = Array.from(nav.querySelectorAll('button')).find((b) =>
        (b.getAttribute('title') || '').startsWith(longTagName),
      );
      if (!btn) return null;
      btn.scrollIntoView({ inline: 'center' });
      const navR = nav.getBoundingClientRect();
      const btnR = btn.getBoundingClientRect();
      const countR = btn.querySelector('.count')!.getBoundingClientRect();
      return {
        navWidth: navR.width,
        btnWidth: btnR.width,
        countRight: countR.right,
        btnRight: btnR.right,
        title: btn.getAttribute('title') || '',
      };
    }, longTag);
    expect(mobileMetrics).not.toBeNull();
    expect(mobileMetrics!.btnWidth).toBeLessThanOrEqual(245);
    expect(mobileMetrics!.btnWidth).toBeLessThan(mobileMetrics!.navWidth);
    expect(mobileMetrics!.countRight).toBeLessThanOrEqual(mobileMetrics!.btnRight + 1);
    expect(mobileMetrics!.title).toContain(longTag);
  } finally {
    await browser.evaluate(async ({ tok, names }) => {
      for (const name of names) {
        await fetch(`/api/v1/profiles/${encodeURIComponent(name)}`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${tok}` },
        }).catch(() => {});
      }
    }, { tok: token, names: createdNames });
  }
});
