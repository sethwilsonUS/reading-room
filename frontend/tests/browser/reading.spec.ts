import { readFile } from 'node:fs/promises';
import { expect, test, type APIRequestContext } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

interface FixtureState {
  work_id: string;
  hit: { id: string; read_url?: string };
  curation_count: number;
}
interface OutlineNode {
  id: string;
  title: string;
  unit_id?: string;
  children?: OutlineNode[];
}

async function source(request: APIRequestContext) {
  const state = (await (await request.get('/__testing/state')).json()) as FixtureState;
  const response = await request.get(`/api/ui/books/${state.work_id}`);
  expect(response.ok()).toBe(true);
  const data = (await response.json()) as { data: { outline: { nodes: OutlineNode[] } } };
  function walk(nodes: OutlineNode[]): OutlineNode[] {
    return nodes.flatMap((node) => [node, ...walk(node.children ?? [])]);
  }
  const node = walk(data.data.outline.nodes).find(
    (item) => item.title === 'A gathering' && item.unit_id,
  );
  expect(node).toBeDefined();
  return { ...state, node: node! };
}

test('passage citation previews and verified exports agree without changing metadata', async ({
  page,
  request,
}) => {
  const initial = await source(request);
  await page.goto(`/read/${initial.hit.id}`);
  await expect(page.locator('main')).toContainText('Copper birds gather beside the river.');
  await expect(page.locator('main script')).toHaveCount(0);
  expect(await page.evaluate(() => 'sourceWasExecuted' in window)).toBe(false);
  await page.getByRole('link', { name: 'Cite this passage', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Cite this passage.' })).toBeVisible();
  await page.getByLabel('Locator type', { exact: true }).selectOption('page');
  await page.getByLabel('Citation location (optional)', { exact: true }).fill('42–43');
  await page.getByRole('checkbox', { name: 'I checked this locator against the source.' }).check();
  await page
    .getByLabel('Locator evidence (required if checked)', { exact: true })
    .fill('Checked the printed page labels in this synthetic test copy.');
  const previewResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/ui/cite/${initial.hit.id}/preview`) &&
      response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Preview this location' }).click();
  const preview = (await (await previewResponse).json()) as {
    data: { citation: { full_note: string; short_note: string; bibliography: string } };
  };
  await expect(
    page.getByRole('textbox', { name: 'Full footnote or endnote — selectable text' }),
  ).toHaveValue(preview.data.citation.full_note);
  expect(preview.data.citation.full_note).toContain('42–43');
  await page.getByLabel('Export this citation').selectOption('json');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download citation' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('citation.json');
  const exported = JSON.parse(await readFile((await download.path())!, 'utf8')) as {
    full_note: string;
    short_note: string;
    bibliography: string;
    locator: { value: string; verified: boolean; evidence: string };
  };
  expect(exported.full_note).toBe(preview.data.citation.full_note);
  expect(exported.short_note).toBe(preview.data.citation.short_note);
  expect(exported.bibliography).toBe(preview.data.citation.bibliography);
  expect(exported.locator.value).toBe('42–43');
  expect(exported.locator.verified).toBe(true);
  expect(exported.locator.evidence).toContain('synthetic test copy');
  expect(
    ((await (await request.get('/__testing/state')).json()) as FixtureState).curation_count,
  ).toBe(initial.curation_count);
  const accessibility = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  expect(accessibility.violations.map((v) => `${v.id}: ${v.description}`)).toEqual([]);
});

test('mouse selection is confirmed before citation and records use only when requested', async ({
  page,
  request,
}) => {
  const initial = await source(request);
  const before = (await (await request.get('/api/history')).json()) as unknown[];
  await page.goto(`/units/${initial.node.unit_id}`);
  const words = page
    .locator('[data-source-text]')
    .filter({ hasText: 'Copper birds gather beside the river.' });
  await expect(words).toBeVisible();
  await words.scrollIntoViewIfNeeded();
  const positions = await words.evaluate((element) => {
    const node = element.firstChild!;
    const first = document.createRange();
    first.setStart(node, 0);
    first.setEnd(node, 1);
    const last = document.createRange();
    last.setStart(node, 11);
    last.setEnd(node, 12);
    const a = first.getBoundingClientRect();
    const b = last.getBoundingClientRect();
    return {
      start: { x: a.left + 0.1, y: a.top + a.height / 2 },
      end: { x: b.right + 0.1, y: b.top + b.height / 2 },
    };
  });
  await page.mouse.move(positions.start.x, positions.start.y);
  await page.mouse.down();
  await page.mouse.move(positions.end.x, positions.end.y, { steps: 16 });
  await page.mouse.up();
  expect(await page.evaluate(() => window.getSelection()?.toString())).toBe('Copper birds');
  await expect(page.getByRole('button', { name: 'Cite selected text', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Cite selected text', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review selected text.' })).toBeVisible();
  await expect(page.locator('blockquote.selection-quotation')).toHaveText('Copper birds');
  const confirmationUrl = page.url();
  await page.getByRole('link', { name: 'Confirm selection and show citation' }).click();
  await expect(page.getByRole('heading', { name: 'Cite selected text.' })).toBeVisible();
  await expect(page.locator('blockquote.selection-quotation').first()).toHaveText('Copper birds');
  expect(((await (await request.get('/api/history')).json()) as unknown[]).length).toBe(
    before.length,
  );
  await page.goto(confirmationUrl);
  await page.getByRole('link', { name: 'Record use of this selection' }).click();
  await page.getByLabel('Purpose', { exact: true }).selectOption('efm');
  await page
    .getByLabel('How did you engage with this source?', { exact: true })
    .selectOption('used');
  await page
    .getByLabel('Your note (optional)', { exact: false })
    .fill('Used these exact copper-bird words in the browser fixture.');
  await page.getByRole('button', { name: 'Save research note' }).click();
  await expect(page.getByText('Your research note was saved.', { exact: true })).toBeVisible();
  const entries = (await (await request.get('/api/history')).json()) as {
    notes?: string;
    target_scope?: string;
    kind?: string;
    target_id?: string;
  }[];
  expect(entries).toHaveLength(before.length + 1);
  const event = entries.find((entry) => entry.notes?.includes('browser fixture'));
  expect(event?.target_scope).toBe('selection');
  expect(event?.kind).toBe('used');
  expect(event?.target_id).toBe(new URL(confirmationUrl).pathname.split('/').at(-1));
});

test('returning from a reading unit restores the originating contents link', async ({
  page,
  request,
}) => {
  const initial = await source(request);
  await page.goto(`/books/${initial.work_id}`);
  const link = page.getByRole('link', { name: 'A gathering', exact: true });
  await expect(link).toBeVisible();
  await link.click();
  await expect(
    page.getByRole('heading', { name: 'A gathering', level: 1, exact: true }),
  ).toBeVisible();
  await page
    .locator('main nav.breadcrumb')
    .getByRole('link', { name: 'The River and the Thread', exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`/books/${initial.work_id}\\?outline_offset=0#outline-`));
  await expect(page.getByRole('link', { name: 'A gathering', exact: true })).toBeFocused();
  await page.reload();
  await expect(page.getByRole('link', { name: 'A gathering', exact: true })).toBeVisible();
});
