import { expect, test, type Page } from '@playwright/test'

import { stubApi } from './fixtures/api'

/**
 * Smoke test: the app boots, calls /health, and renders the result.
 *
 * The API is stubbed so the test stays hermetic — `docker compose up` plus a
 * green /health is verified separately, against the real stack.
 *
 * The page sits behind authentication from M1: it renders the raw error text
 * from Postgres, Redis and S3, and an anonymous visitor should not learn a
 * backing service's hostname from a failed connection message.
 */

async function signInThenOpenStatus(page: Page) {
  await page.goto('/login')
  await page.getByLabel('Email').fill('owner@example.com')
  await page.getByLabel('Password').fill('correct-horse-battery-staple')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  await page.goto('/settings/system')
}

const HEALTH_ROUTE = '**/api/v1/health'

function healthPayload(overrides: {
  status: 'ok' | 'degraded'
  redisStatus: 'ok' | 'error'
  redisError: string | null
}) {
  return {
    status: overrides.status,
    service: 'Configurable CRM API',
    version: '0.1.0',
    environment: 'test',
    checks: {
      database: { status: 'ok', latency_ms: 1.5, error: null },
      redis: {
        status: overrides.redisStatus,
        latency_ms: 0.9,
        error: overrides.redisError,
      },
      object_storage: { status: 'ok', latency_ms: 4.2, error: null },
    },
  }
}

async function stubHealth(page: Page, statusCode: number, body: unknown) {
  await page.route(HEALTH_ROUTE, async (route) => {
    await route.fulfill({
      status: statusCode,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  })
}

test('renders a green report when every backing service is healthy', async ({ page }) => {
  await stubApi(page)
  // Registered after the catch-all so it wins for /health.
  await stubHealth(page, 200, healthPayload({ status: 'ok', redisStatus: 'ok', redisError: null }))

  await signInThenOpenStatus(page)

  await expect(page.getByTestId('overall-status')).toHaveText('All systems operational')
  await expect(page.getByTestId('service-identity')).toContainText('Configurable CRM API')

  for (const check of ['database', 'redis', 'object_storage']) {
    await expect(page.getByTestId(`check-${check}`).getByTestId('check-status')).toHaveText('ok')
  }
})

test('renders a degraded report and the failure detail', async ({ page }) => {
  await stubApi(page)
  await stubHealth(
    page,
    503,
    healthPayload({
      status: 'degraded',
      redisStatus: 'error',
      redisError: 'ConnectionError: connection refused',
    }),
  )

  await signInThenOpenStatus(page)

  await expect(page.getByTestId('overall-status')).toHaveText('Degraded')
  await expect(page.getByTestId('check-redis').getByTestId('check-status')).toHaveText('error')
  await expect(page.getByTestId('check-redis').getByTestId('check-error')).toContainText(
    'connection refused',
  )
})

test('the status page is not reachable without signing in', async ({ page }) => {
  await stubApi(page)
  await page.goto('/status')
  await expect(page).toHaveURL(/\/login/)
})
