/**
 * The dashboard, its charts and the editor (M9).
 *
 * Two of these check things only a browser can, and both come straight from
 * the dataviz method:
 *
 * **Identity never depends on the chart rendering.** Every chart carries a
 * table view, so the numbers survive colour-blindness, a print, and a failed
 * SVG alike.
 *
 * **A single series gets no legend.** A box with one swatch restates the title
 * and costs space — so its *absence* is the assertion.
 *
 * The rest are the drill-through (a chart that disagrees with the list behind
 * it is worse than no chart, because it is believed) and the editor rendering
 * a config form from the *served* schema rather than a hardcoded one.
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

async function openDashboard(page: Page, options = {}) {
  const stub = await signIn(page, options)
  await page.getByRole('link', { name: 'Dashboard', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Morning check' })).toBeVisible()
  return stub
}

test.describe('the dashboard', () => {
  test('follow-ups are stat tiles, not a three-bar chart', async ({ page }) => {
    await openDashboard(page)
    // The dataviz form table sends "a handful of headline numbers" to a KPI
    // row. A chart here would be a chart drawn because charts were available.
    const tiles = page.getByTestId('stat-tile')
    await expect(tiles).toHaveCount(3)
    await expect(tiles.first()).toContainText('Late')
    await expect(tiles.first()).toContainText('3')
  })

  test('a single-series bar chart carries no legend', async ({ page }) => {
    await openDashboard(page)
    // One colour, so the title already says what is plotted.
    await expect(page.getByText('Leads by stage')).toBeVisible()
    await expect(page.getByTestId('bar')).not.toHaveCount(0)
    await expect(page.getByRole('list', { name: /legend/i })).toHaveCount(0)
  })

  test('every chart has a table view', async ({ page }) => {
    await openDashboard(page)
    // Identity must never depend on the chart rendering at all.
    const chart = page.locator('figure', { hasText: 'Leads by stage' })
    await chart.getByRole('button', { name: 'View as table' }).click()
    await expect(chart.getByRole('table')).toBeVisible()
    await expect(chart.getByRole('cell', { name: 'Contacted' })).toBeVisible()
    await expect(chart.getByRole('cell', { name: '11' })).toBeVisible()
  })

  test('values are labelled on the bars themselves', async ({ page }) => {
    await openDashboard(page)
    // Direct labels before gridlines — with every bar labelled there is
    // nothing left for an axis to say.
    const chart = page.locator('figure', { hasText: 'Leads by stage' })
    await expect(chart).toContainText('18')
    await expect(chart).toContainText('New')
  })

  test('every bar is announced with its label and value', async ({ page }) => {
    await openDashboard(page)
    // Found by reading the accessibility tree of the real page: the bar is
    // aria-hidden and the surrounding spans composed no name, so a screen
    // reader announced seven identical "button"s.
    await expect(page.getByRole('button', { name: /^New: 18 — show these leads$/ })).toBeVisible()
  })

  test('clicking a bar drills through to that exact bucket', async ({ page }) => {
    await openDashboard(page)
    const chart = page.locator('figure', { hasText: 'Leads by stage' })
    await chart.getByTestId('bar').first().click()
    // The chart's number and the list behind it have to agree, so the bucket
    // key is what travels — not its label.
    await expect(page).toHaveURL(/\/leads\?stage_id=stage-1/)
  })

  test('the leaderboard shows only the metrics the workspace turned on', async ({ page }) => {
    await openDashboard(page)
    const rows = page.getByTestId('leaderboard-row')
    await expect(rows).toHaveCount(2)
    await expect(page.getByRole('columnheader', { name: 'Leads' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Calls' })).toBeVisible()
    // Rating is off in this workspace, so its column is absent entirely.
    await expect(page.getByRole('columnheader', { name: 'Avg rating' })).toHaveCount(0)
  })

  test('the date range is a preset list, not two date boxes', async ({ page }) => {
    await openDashboard(page)
    const range = page.getByLabel('Date range')
    await expect(range).toBeVisible()
    await expect(range.getByRole('option')).toHaveCount(4)
  })

  test('a refused request is not an empty dashboard', async ({ page }) => {
    const stub = await signIn(page, { reportsAllowed: false })
    await page.getByRole('link', { name: 'Dashboard', exact: true }).click()
    await expect(page.getByRole('alert').first()).toContainText(
      /permission template does not allow/,
    )
    expect(stub.requests.some((path) => path.includes('/dashboards'))).toBe(true)
  })
})

test.describe('the dashboard editor', () => {
  test('the widget list comes from the server', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/dashboards')
    await expect(page.getByRole('heading', { name: 'Dashboards' })).toBeVisible()

    // A hardcoded list here would need a release every time the backend grew a
    // widget.
    await expect(page.getByText('Breakdown by field')).toBeVisible()
    await expect(page.getByText('Group leads by any field you can view.')).toBeVisible()
  })

  test('the catalogue names kinds of chart, never subjects', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/dashboards')
    // There is no "leads by source" widget, because which field means source
    // is the customer's decision.
    for (const forbidden of ['by source', 'by course', 'by status']) {
      await expect(page.getByText(forbidden, { exact: false })).toHaveCount(0)
    }
  })

  test('a breakdown widget asks which field, from the served schema', async ({ page }) => {
    const stub = await signIn(page)
    await page.goto('/settings/dashboards')
    await page.getByRole('button', { name: 'New dashboard' }).click()

    await page.getByLabel('Name').fill('Channels')
    await page
      .getByRole('group', { name: 'Widgets' })
      .getByRole('checkbox', { name: /Breakdown by field/ })
      .check()

    // The config form is generated — the editor does not know what a breakdown
    // needs until the catalogue tells it.
    const groupBy = page.getByLabel('Group by')
    await expect(groupBy).toBeVisible()
    await expect(page.getByText(/which one means 'source' is your call/)).toBeVisible()

    // Required and unset, so the save is blocked rather than saving something
    // that would render as a blank tile.
    await expect(page.getByRole('button', { name: 'Create dashboard' })).toBeDisabled()

    await groupBy.selectOption({ index: 1 })
    await page.getByRole('button', { name: 'Create dashboard' }).click()

    await expect.poll(() => stub.lastDashboard()?.['name']).toBe('Channels')
    const layout = (await stub.lastDashboard()?.['layout']) as Array<Record<string, unknown>>
    expect(layout[0]?.['widget']).toBe('breakdown')
    expect((layout[0]?.['config'] as Record<string, string>)['field_key']).toBeTruthy()
  })

  test('binding to a template is offered as giving it to a role', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/dashboards')
    await page.getByRole('button', { name: 'New dashboard' }).click()

    const roles = page.getByLabel('Give it to a role')
    await expect(roles).toBeVisible()
    // One admin action rather than one per person. Options inside a native
    // select are never "visible" to Playwright, so this asserts they exist.
    await expect(roles.getByRole('option', { name: /Everyone on/ })).not.toHaveCount(0)
    await expect(roles).toContainText('Everyone on Root')
  })

  test('an existing dashboard lists its widget count', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/dashboards')
    const row = page.getByTestId('dashboard-row').first()
    await expect(row).toContainText('Morning check')
    await expect(row).toContainText('3 widgets')
    await expect(row).toContainText('Default')
  })
})
