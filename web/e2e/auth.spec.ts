import { expect, test } from '@playwright/test'

import { membershipSummary, stubApi, WORKSPACE_A, WORKSPACE_B } from './fixtures/api'

test.describe('sign in', () => {
  test('signs in and lands in the only workspace', async ({ page }) => {
    await stubApi(page)
    await page.goto('/login')

    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
    await expect(page.getByRole('complementary').getByText('Acme Sales')).toBeVisible()
  })

  test('shows the licence message rather than a generic failure', async ({ page }) => {
    await stubApi(page, { loginResult: 'no_license' })
    await page.goto('/login')

    await page.getByLabel('Email').fill('unlicensed@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('alert')).toContainText('no licensed seat')
    await expect(page).toHaveURL(/\/login/)
  })

  test('shows the deactivated message', async ({ page }) => {
    await stubApi(page, { loginResult: 'member_inactive' })
    await page.goto('/login')

    await page.getByLabel('Email').fill('departed@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('alert')).toContainText('deactivated')
  })

  test('does not reveal whether an email exists', async ({ page }) => {
    await stubApi(page, { loginResult: 'invalid_credentials' })
    await page.goto('/login')

    await page.getByLabel('Email').fill('nobody@example.com')
    await page.getByLabel('Password').fill('whatever')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('alert')).toHaveText('Email or password is incorrect.')
  })
})

test.describe('workspace picker', () => {
  test('asks which workspace when there is more than one', async ({ page }) => {
    await stubApi(page, {
      memberships: [
        membershipSummary(),
        membershipSummary({
          id: 'bbbbbbbb-0000-0000-0000-000000000009',
          workspace: {
            id: WORKSPACE_B,
            name: 'Second Tenant',
            slug: 'second-tenant',
            timezone: 'America/New_York',
            currency: 'USD',
            default_country_code: '1',
          },
        }),
      ],
    })

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('heading', { name: 'Choose a workspace' })).toBeVisible()
    await expect(page.getByText('Acme Sales')).toBeVisible()
    await expect(page.getByText('Second Tenant')).toBeVisible()

    await page.getByRole('button', { name: 'Open Second Tenant' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  })

  test('one usable workspace is entered without asking', async ({ page }) => {
    // A picker with a single answer is a speed bump, so login goes straight in
    // when exactly one membership is both active and licensed.
    await stubApi(page, {
      memberships: [
        membershipSummary(),
        membershipSummary({
          id: 'bbbbbbbb-0000-0000-0000-000000000009',
          has_license: false,
          workspace: {
            id: WORKSPACE_B,
            name: 'Locked Out',
            slug: 'locked-out',
            timezone: 'Asia/Kolkata',
            currency: 'INR',
            default_country_code: '91',
          },
        }),
      ],
    })

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()

    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  })

  test('an unlicensed membership is listed but not selectable', async ({ page }) => {
    // Listed with a reason rather than hidden — "where did my workspace go?"
    // is the support ticket that follows hiding it.
    await stubApi(page, {
      memberships: [
        membershipSummary(),
        membershipSummary({
          id: 'bbbbbbbb-0000-0000-0000-000000000009',
          has_license: false,
          workspace: {
            id: WORKSPACE_B,
            name: 'Locked Out',
            slug: 'locked-out',
            timezone: 'Asia/Kolkata',
            currency: 'INR',
            default_country_code: '91',
          },
        }),
      ],
    })

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()

    await page.goto('/workspaces')
    await expect(page.getByRole('button', { name: 'Open Locked Out' })).toBeDisabled()
    await expect(page.getByText('No licensed seat')).toBeVisible()
  })
})

test.describe('session', () => {
  test('an expired access token is refreshed once and the call retried', async ({ page }) => {
    const stub = await stubApi(page)

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()

    // Expire the token, then force the page to refetch.
    stub.expireAccessToken()
    await page.reload()

    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
    expect(stub.refreshCount()).toBeGreaterThanOrEqual(1)
  })

  test('several parallel 401s trigger only one refresh', async ({ page }) => {
    // The server revokes a refresh token's whole family if a rotated token is
    // replayed. Two concurrent refreshes look exactly like that, so the client
    // must coalesce them — otherwise a page load with three queries logs the
    // user out.
    const stub = await stubApi(page)

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()

    stub.expireAccessToken()
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()

    // The members page issues /me, /me/permissions, /members, /members/seats
    // and /settings/permission-templates concurrently. One refresh, not five.
    expect(stub.refreshCount()).toBe(1)
  })

  test('signing out returns to the login page', async ({ page }) => {
    await stubApi(page)

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()

    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page).toHaveURL(/\/login/)

    // The session must not survive a reload.
    await page.goto('/members')
    await expect(page).toHaveURL(/\/login/)
  })

  test('an unauthenticated visit to a tenant route redirects to login', async ({ page }) => {
    await stubApi(page)
    await page.goto(`/members`)
    await expect(page).toHaveURL(/\/login/)
  })
})

test.describe('workspace scoping', () => {
  test('every tenant request is scoped to the active workspace', async ({ page }) => {
    const stub = await stubApi(page)

    await page.goto('/login')
    await page.getByLabel('Email').fill('owner@example.com')
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()

    // The heading paints before any fetch resolves, so sampling the recorded
    // requests right here raced the first one and failed about one run in
    // three. Poll until the shell has actually asked for something.
    await expect
      .poll(() => stub.requests.filter((entry) => entry.includes('/workspaces/')).length)
      .toBeGreaterThan(0)

    const tenantCalls = stub.requests.filter((entry) => entry.includes('/workspaces/'))
    for (const call of tenantCalls) {
      expect(call).toContain(WORKSPACE_A)
      expect(call).not.toContain(WORKSPACE_B)
    }
  })
})
