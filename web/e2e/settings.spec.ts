import { expect, test, type Page } from '@playwright/test'

import { leadField, stubApi, type StubOptions } from './fixtures/api'

/**
 * The M2-M4 settings surface.
 *
 * The assertion that matters most here is the first one: the type picker is
 * populated from the server registry. If someone ever hardcodes a type list in
 * the frontend, that test keeps passing by accident — so it counts the options
 * against the stub's registry rather than looking for particular names.
 */

async function signIn(page: Page, options: StubOptions = {}) {
  const stub = await stubApi(page, options)
  await page.goto('/login')
  await page.getByLabel('Email').fill('owner@example.com')
  await page.getByLabel('Password').fill('correct-horse-battery-staple')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  return stub
}

test.describe('field settings (M2)', () => {
  test('the type picker is built from the server registry, not a local list', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/fields')
    await expect(page.getByRole('heading', { name: 'Lead fields' })).toBeVisible()

    await page.getByRole('button', { name: 'Add a new field' }).click()
    const picker = page.getByLabel('Type', { exact: true })

    // 13 types, straight from the stubbed registry. A frontend that shipped its
    // own list would drift from this the moment the backend gained a type.
    await expect(picker.locator('option')).toHaveCount(13)
    await expect(picker.locator('option', { hasText: 'Dependent dropdown' })).toHaveCount(1)
    await expect(picker.locator('option', { hasText: 'Recurring date' })).toHaveCount(1)
    await expect(picker.locator('option', { hasText: 'Location' })).toHaveCount(1)
  })

  test('the four built-in fields are listed with their keys', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/fields')

    const rows = page.getByTestId('field-row')
    await expect(rows).toHaveCount(4)
    await expect(page.getByText('built-in').first()).toBeVisible()
    // The key is shown because it is what stored values are filed under, and it
    // never changes when the label does.
    await expect(page.getByText('alternate_phone')).toBeVisible()
  })

  test('creating a field posts the type the registry named', async ({ page }) => {
    const stub = await signIn(page)
    await page.goto('/settings/fields')
    await page.getByRole('button', { name: 'Add a new field' }).click()

    await page.getByLabel('Name', { exact: true }).fill('Territory')
    await page.getByLabel('Type', { exact: true }).selectOption('DEPENDENT_DROPDOWN')
    await page.getByRole('button', { name: 'Create field' }).click()

    await expect
      .poll(() => stub.requests.filter((entry) => entry.includes('POST /workspaces')).length)
      .toBeGreaterThan(0)
    await expect(page.getByTestId('field-row')).toHaveCount(5)
  })

  test('the search and type filters narrow the list', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/fields')

    await page.getByLabel('Search fields').fill('phone')
    await expect(page.getByTestId('field-row')).toHaveCount(2)

    await page.getByLabel('Search fields').fill('')
    await page.getByLabel('Filter by type').selectOption('EMAIL')
    await expect(page.getByTestId('field-row')).toHaveCount(1)
  })

  test('a dependent dropdown offers a parent picker in its option editor', async ({ page }) => {
    // A cascade field with one parent option already in place.
    const cascade = leadField({
      id: 'field-5',
      key: 'territory',
      label: 'Territory',
      field_type: 'DEPENDENT_DROPDOWN',
      is_builtin: false,
      options: [
        {
          id: 'option-1',
          code: 'north',
          label: 'North',
          color: null,
          sort_order: 0,
          is_archived: false,
          parent_option_id: null,
        },
      ],
    })
    await signIn(page, { fields: [cascade] })
    await page.goto('/settings/fields')
    await page.getByRole('button', { name: 'Edit' }).first().click()

    await expect(page.getByTestId('option-editor')).toBeVisible()
    // The parent picker is what makes the cascade configurable at all.
    const parentPicker = page.getByLabel('Parent option')
    await expect(parentPicker).toBeVisible()
    await expect(parentPicker.locator('option', { hasText: 'under North' })).toHaveCount(1)
  })

  test('the identity field picker lists the workspace fields', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/fields')

    const identity = page.getByLabel('Lead Id')
    await expect(identity).toBeVisible()
    // Phone is the provisioned identity field; which field it is remains a
    // per-workspace setting, so the control is a picker rather than a label.
    await expect(identity).toHaveValue('field-2')
  })
})

test.describe('pipeline settings (M3)', () => {
  test('renders the three-column pipeline with its cardinality note', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/pipeline')
    await expect(page.getByRole('heading', { name: 'Pipeline and taxonomy' })).toBeVisible()

    await expect(page.getByText('Initial stage')).toBeVisible()
    await expect(page.getByText('Closed stages')).toBeVisible()
    // One initial, one active, one won, one lost.
    await expect(page.getByTestId('stage-chip')).toHaveCount(4)
    await expect(page.getByText('One won, one lost')).toBeVisible()
  })

  test('a system disposition cannot be renamed but a custom one can', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/pipeline')

    await expect(page.getByLabel('Disposition Connected')).toBeDisabled()
    await expect(page.getByLabel('Disposition Left Voicemail')).toBeEnabled()
    await expect(page.getByText('system').first()).toBeVisible()
  })

  test('the lost-reason cap is shown against the live count', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/pipeline')
    await expect(page.getByText('(3/25)')).toBeVisible()
  })

  test('feature flags are presented as endpoint-level switches', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/pipeline')

    await expect(page.getByText('A disabled feature’s API refuses with 403')).toBeVisible()
    await expect(page.getByLabel('custom actions')).toBeChecked()
  })

  test('a stage cardinality refusal surfaces as guidance, not a raw 409', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/pipeline')

    await page.getByLabel('New stage name').fill('Qualified')
    await page.getByRole('button', { name: 'Add', exact: true }).first().click()

    await expect(page.getByRole('alert')).toContainText('exactly one initial, won and lost')
  })
})

test.describe('custom actions (M3)', () => {
  test('lists an action with its code, score and direction', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/custom-actions')

    await expect(page.getByRole('heading', { name: 'Custom actions' })).toBeVisible()
    await expect(page.getByTestId('custom-action-card')).toHaveCount(1)
    await expect(page.getByText('#1001')).toBeVisible()
    await expect(page.getByText('+50')).toBeVisible()
    // Every action starts with a required Notes field.
    await expect(page.getByText('Notes', { exact: true })).toBeVisible()
  })

  test('a disabled feature explains itself instead of showing an empty list', async ({ page }) => {
    await signIn(page, { customActionsEnabled: false })
    await page.goto('/settings/custom-actions')

    // CardTitle is a styled div, not a semantic heading, so match on text.
    await expect(page.getByText('Custom actions are switched off')).toBeVisible()
    await expect(page.getByText('refuses these endpoints with 403')).toBeVisible()
  })
})

test.describe('permissions (M4)', () => {
  test('the field matrix shows four grants with server-computed rollups', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/permissions')
    await expect(page.getByRole('heading', { name: 'Permission templates' })).toBeVisible()

    await expect(page.getByTestId('matrix-row')).toHaveCount(4)
    // Export defaults to none — the data-exfiltration control from §6.4.
    const matrix = page.getByTestId('field-matrix')
    await expect(matrix.getByText('None')).toBeVisible()
    await expect(matrix.getByText('Partial')).toBeVisible()
    await expect(page.getByText('Export defaults to none')).toBeVisible()
  })

  test('proposed capability groups are flagged as proposed', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/permissions')

    // Leads was observed; the rest were proposed by this codebase and say so.
    await expect(page.getByText('proposed').first()).toBeVisible()
  })

  test('the matrix can be filtered by field name', async ({ page }) => {
    await signIn(page)
    await page.goto('/settings/permissions')

    await page.getByLabel('Search fields in matrix').fill('Email')
    await expect(page.getByTestId('matrix-row')).toHaveCount(1)
  })
})

test.describe('the settings door', () => {
  test('configuration screens live behind Settings, not in the main nav', async ({ page }) => {
    await signIn(page)

    // The redesign took ten configuration and deployment entries out of the
    // primary nav. A client sitting through a demo should not be one stray
    // click away from the permission matrix or the dependency health page.
    await expect(page.getByRole('link', { name: 'Fields' })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Permissions' })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'System' })).toHaveCount(0)

    await page.getByRole('link', { name: 'Settings' }).click()
    await expect(page).toHaveURL(/\/settings$/)

    // Scoped to the rail: the overview also links to Fields, and an unscoped
    // query would match both.
    await page
      .getByRole('navigation', { name: 'Settings' })
      .getByRole('link', { name: 'Fields' })
      .click()
    await expect(page).toHaveURL(/\/settings\/fields$/)
    await expect(page.getByRole('heading', { name: 'Lead fields' })).toBeVisible()
  })

  test('the old /status bookmark still lands on the system page', async ({ page }) => {
    await signIn(page)
    await page.goto('/status')
    await expect(page).toHaveURL(/\/settings\/system$/)
  })
})
