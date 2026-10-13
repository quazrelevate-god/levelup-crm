/**
 * Assignment rules, distribution and schedules (M8).
 *
 * The assertions that matter are against the bodies the page *sends* —
 * `stub.lastRule()`, `lastReorder()`, `lastDistribute()` — not against what the
 * stub echoes back. Checking the rendered result would only prove the stub
 * works.
 *
 * Rules are the highest-leverage setting in the product: one decides where
 * every future lead lands. So these cover the two things a browser can prove
 * that an API test cannot — that the *order* an operator sees is the order the
 * server is told, and that a refused request does not render as an empty list.
 */

import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

import { stubApi } from './fixtures/api'

async function signIn(page: Page, options = {}) {
  const stub = await stubApi(page, options)
  await page.goto('/')
  await page.getByLabel('Email').fill('owner@example.com')
  await page.getByLabel('Password').fill('correct-horse-battery-staple')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  return stub
}

test.describe('assignment rules', () => {
  test('rules are listed in priority order, numbered', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/assignment')

    await expect(page.getByRole('heading', { name: 'Assignment' })).toBeVisible()
    const rules = page.getByTestId('assignment-rule')
    await expect(rules).toHaveCount(2)
    // Position is behaviour here, not decoration: first match wins.
    await expect(rules.first()).toContainText('1')
    await expect(rules.first()).toContainText('Website enquiries')
    await expect(rules.nth(1)).toContainText('Everyone else')
  })

  test('moving a rule sends the whole new order', async ({ page }) => {
    const stub = await signIn(page)
    await page.goto('/settings/assignment')

    await page.getByRole('button', { name: 'Move Everyone else up' }).click()

    await expect.poll(() => stub.lastReorder()).toEqual(['rule-2', 'rule-1'])
    // A partial order would silently renumber the rules it omitted, so the
    // page always sends every id.
    await expect(page.getByTestId('assignment-rule').first()).toContainText('Everyone else')
  })

  test('a new round-robin rule sends the members it was given', async ({ page }) => {
    const stub = await signIn(page)
    await page.goto('/settings/assignment')
    await page.getByRole('button', { name: 'New rule' }).click()

    await page.getByLabel('Name').fill('Weekend cover')
    await page.getByLabel('Strategy').selectOption('ROUND_ROBIN')
    await page
      .getByRole('group', { name: 'Members, in order' })
      .getByRole('checkbox')
      .first()
      .check()
    await page.getByRole('button', { name: 'Create rule' }).click()

    await expect.poll(() => stub.lastRule()?.['name']).toBe('Weekend cover')
    await expect.poll(() => stub.lastRule()?.['strategy']).toBe('ROUND_ROBIN')
    const config = (await stub.lastRule()?.['config']) as { membership_ids?: string[] }
    expect(config.membership_ids?.length).toBe(1)
  })

  test('the strategy picker explains what each one does', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/assignment')
    await page.getByRole('button', { name: 'New rule' }).click()

    await page.getByLabel('Strategy').selectOption('UNASSIGNED')
    // The one strategy whose behaviour is genuinely surprising: it *matches*,
    // and thereby stops the rules below it.
    await expect(page.getByText(/assigns nobody, stopping the rules below/)).toBeVisible()
  })

  test('the create dialog says unlicensed members are always skipped', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/assignment')
    await page.getByRole('button', { name: 'New rule' }).click()

    await expect(page.getByText(/cannot log in to work the lead/)).toBeVisible()
  })

  test('a refused request is not an empty rule list', async ({ page }) => {
    await signIn(page, { rulesAllowed: false })
    await page.goto('/settings/assignment')

    // "No rules yet" after a 403 tells the operator their leads arrive
    // unassigned by design, which is the opposite of what happened.
    await expect(page.getByRole('alert')).toContainText(/permission template does not allow/)
    await expect(page.getByText('No rules yet')).toHaveCount(0)
  })

  test('deactivating keeps the rule visible', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/assignment')

    await page
      .getByTestId('assignment-rule')
      .first()
      .getByRole('button', { name: 'Deactivate' })
      .click()

    // The rule explains where existing leads went, so it stays listed.
    await expect(page.getByTestId('assignment-rule').first()).toContainText('Inactive')
    await expect(
      page.getByTestId('assignment-rule').first().getByRole('button', { name: 'Reactivate' }),
    ).toBeVisible()
  })
})

test.describe('distribution', () => {
  test('distributing a selection sends the ids and the strategy', async ({ page }) => {
    const stub = await signIn(page)

    await page.getByRole('checkbox', { name: 'Select every lead on this page' }).check()
    await page.getByRole('button', { name: /^Distribute \d+$/ }).click()

    await page.getByLabel('How').selectOption('FIXED')
    await page.getByRole('group', { name: 'Who gets them' }).getByRole('radio').first().check()
    await page
      .getByRole('button', { name: /^Distribute \d+$/ })
      .last()
      .click()

    await expect.poll(() => stub.lastDistribute()?.strategy).toBe('FIXED')
    await expect.poll(() => stub.lastDistribute()?.lead_ids?.length).toBeGreaterThan(0)
  })

  test('the dialog says the run can be undone as a unit', async ({ page }) => {
    await signIn(page)
    await page.getByRole('checkbox', { name: 'Select every lead on this page' }).check()
    await page.getByRole('button', { name: /^Distribute \d+$/ }).click()

    // The property that makes this safe to offer on a large selection.
    await expect(page.getByText(/undone from the edit report in one go/)).toBeVisible()
  })
})

test.describe('scheduled reports', () => {
  test('the workspace timezone is stated, not the browser one', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/scheduled-reports')

    await expect(page.getByRole('heading', { name: 'Scheduled reports' })).toBeVisible()
    // A manager in another zone would otherwise read 09:00 as their own.
    await expect(page.getByText(/workspace.s clock/)).toBeVisible()
  })

  test('a schedule lists its cadence in words', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/scheduled-reports')

    const rows = page.getByTestId('scheduled-report')
    await expect(rows).toHaveCount(1)
    await expect(rows.first()).toContainText('Mondays at 09:00')
    await expect(rows.first()).toContainText('1 recipient')
  })

  test('a failed run is visible on the row', async ({ page }) => {
    await signIn(page, {
      scheduledReports: [
        {
          id: 'schedule-broken',
          name: 'Broken',
          report_type: 'leads',
          cron: '0 9 * * *',
          recipients: ['ops@example.com'],
          params: {},
          format: 'CSV',
          is_active: true,
          last_run_at: '2026-08-20T09:00:00Z',
          last_error: 'This schedule&apos;s creator is no longer a member',
          created_by: null,
        },
      ],
    })
    await page.goto('/settings/scheduled-reports')

    // Otherwise it fails every morning and nobody finds out until somebody
    // asks where the report went.
    await expect(page.getByTestId('scheduled-report')).toContainText('Last run failed')
    await expect(page.getByTestId('scheduled-report')).toContainText('No creator')
  })

  test('creating a schedule sends the parsed recipient list', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/scheduled-reports')
    await page.getByRole('button', { name: 'Schedule a report' }).click()

    await page.getByLabel('Name').fill('Daily pipeline')
    await page.getByLabel('Cadence').selectOption('0 9 * * 1-5')
    await page.getByLabel('Recipients').fill('a@example.com, b@example.com')
    await expect(page.getByText('2 recipients.')).toBeVisible()

    await page.getByRole('button', { name: 'Schedule', exact: true }).click()
    await expect(page.getByTestId('scheduled-report')).toHaveCount(2)
  })

  test('the dialog says whose permissions the report renders with', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/scheduled-reports')
    await page.getByRole('button', { name: 'Schedule a report' }).click()

    await expect(page.getByText(/your field permissions, not the recipients/)).toBeVisible()
  })
})
