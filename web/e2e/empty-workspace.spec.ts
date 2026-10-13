/**
 * Configure an empty workspace, then use it (M11).
 *
 * The milestone's acceptance criterion, and the only spec in this repo that
 * runs against the **real backend**. Every other one stubs every API route,
 * which is right for asserting what a page sends — and structurally unable to
 * prove the thing this product actually claims:
 *
 *   > Nothing about any customer's business is hardcoded. Every field, stage,
 *   > status and disposition is created by a workspace admin at runtime.
 *
 * A stub cannot prove that, because a stub is where the hardcoding would hide.
 * So this one starts from a workspace containing only what provisioning
 * creates — four built-in fields, four stages, no taxonomy — builds a business
 * through the settings UI, and then works a lead through it.
 *
 * **Skipped unless `E2E_LIVE=1`.** CI's web job has no database, and a suite
 * that fails without one would be a suite people learn to ignore. Run it with:
 *
 *   ops/e2e-fixture.sh                     # build the empty workspace
 *   # start the API against crm_e2e
 *   ops/e2e-fixture.sh --redeem
 *   E2E_LIVE=1 pnpm exec playwright test empty-workspace
 */

import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

const LIVE = process.env.E2E_LIVE === '1'
const EMAIL = process.env.E2E_EMAIL ?? 'founder@example.com'
const PASSWORD = process.env.E2E_PASSWORD ?? 'drill-password-for-the-e2e'

test.describe('an empty workspace', () => {
  test.skip(!LIVE, 'needs a real API and database — see ops/e2e-fixture.sh')
  // One journey, in order: each step depends on the configuration the previous
  // one created, which is the point.
  test.describe.configure({ mode: 'serial' })

  let page: Page

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage()
    await page.goto('/')
    await page.getByLabel('Email').fill(EMAIL)
    await page.getByLabel('Password').fill(PASSWORD)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  })

  test.afterAll(async () => {
    await page.close()
  })

  test('starts with no business vocabulary at all', async () => {
    await page.goto('/settings/fields')

    // The four built-ins and nothing else. If a fixture ever seeds a taxonomy
    // into a real workspace, this is where it shows up.
    const body = (await page.locator('body').innerText()).toLowerCase()
    for (const word of ['course', 'student', 'enquiry', 'tutor', 'admission']) {
      expect(body, `a fresh workspace mentions ${word}`).not.toContain(word)
    }
  })

  test('an admin invents a field, and it reaches the create form', async () => {
    await page.goto('/settings/fields')
    await page.getByRole('button', { name: 'Add a new field' }).click()

    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Name', { exact: true }).fill('Enquiry channel')
    // "Show in quick add" is what carries a new field onto the create form.
    // Without it the field exists and the journey stops here, which would make
    // this test prove configuration and not use.
    await dialog.getByLabel(/Show in quick add/).check()
    await dialog.getByRole('button', { name: 'Create field' }).click()

    await expect(dialog).toHaveCount(0)
    await expect(page.getByRole('cell', { name: 'Enquiry channel' })).toBeVisible()
  })

  test('and a stage in their own words', async () => {
    await page.goto('/settings/pipeline')
    // Inline, not a dialog: the pipeline screen is three columns of editable
    // rows, which is why there is no "new stage" modal to open.
    await page.getByPlaceholder('New stage').fill('Awaiting callback')
    await page.getByPlaceholder('New stage').locator('xpath=following-sibling::button[1]').click()

    await expect(page.locator("input[value='Awaiting callback']")).toBeVisible()
  })

  test('then a lead can be created using that field', async () => {
    await page.getByRole('link', { name: 'Leads', exact: true }).click()
    await page.getByRole('button', { name: 'Add lead' }).click()

    const dialog = page.getByRole('dialog')
    // The field an admin invented two steps ago is on the form. Nothing in the
    // frontend knew the words "Enquiry channel" before somebody typed them —
    // which is the claim this whole product rests on.
    await expect(dialog.getByLabel('Enquiry channel')).toBeVisible()

    // Exact: "Phone" also matches the built-in "Alternate Phone".
    await dialog.getByLabel('Phone', { exact: true }).fill('+919000000001')
    await dialog.getByLabel('Name', { exact: true }).fill('First Ever Lead')
    await dialog.getByLabel('Enquiry channel').fill('Walk-in')
    await dialog.getByRole('button', { name: 'Create lead' }).click()

    await expect(dialog).toHaveCount(0)
    await expect(page.getByText('+919000000001')).toBeVisible()
  })

  test('and the value survives a round trip', async () => {
    await page.getByRole('link', { name: 'Leads', exact: true }).click()
    // The identifier cell, not the row: the first cell holds the select
    // checkbox and stops propagation, so a click on the row's centre can land
    // somewhere that deliberately does not open the detail.
    await page.getByRole('cell', { name: '+919000000001', exact: true }).click()

    // Stored under a key derived from the label the admin chose, read back
    // through the field-projection service, and rendered by a form built from
    // the workspace's own schema — none of which existed before this test
    // typed the words in.
    // The panel opens before the workspace's schema has loaded, so this waits
    // rather than asserting on the first frame.
    const field = page.getByTestId('lead-field').filter({ hasText: 'Enquiry channel' })
    await expect(field).toBeVisible({ timeout: 15_000 })
    // The value is an input's `value`, not a text node, so it is read as one.
    await expect(field.locator('input')).toHaveValue('Walk-in')
  })
})
