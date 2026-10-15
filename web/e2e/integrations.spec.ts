/**
 * API keys, webhooks, the outbox and the intake log (M10).
 *
 * Two properties here can only be checked in a browser, and both are the sort
 * of thing that goes wrong silently:
 *
 * **A secret is shown once, and the page has to say so.** If the reveal panel
 * read like an ordinary field, an operator would close it expecting to come
 * back — and there is nothing to come back to, because only a hash is stored.
 *
 * **A warning in the intake log is the only trace an unknown field leaves.**
 * The API deliberately accepts it, so if this screen does not surface the
 * warning, nobody ever learns the integration is sending something the
 * workspace has no field for.
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
  await page.goto('/settings/integrations')
  await expect(page.getByRole('heading', { name: 'Integrations' })).toBeVisible()
  return stub
}

test.describe('api keys', () => {
  test('a key is listed by prefix, never in full', async ({ page }) => {
    await signIn(page)
    const rows = page.getByTestId('api-key')
    await expect(rows).toHaveCount(1)
    await expect(rows.first()).toContainText('Website form')
    // The prefix identifies it; the rest is a hash on the server.
    await expect(rows.first()).toContainText('crmk_existing')
  })

  test('the plaintext appears once, and the page says so', async ({ page }) => {
    const stub = await signIn(page)
    await page.getByRole('button', { name: 'New key' }).click()

    await page.getByLabel('Name').fill('Voice agent')
    await page.getByLabel('Permission template').selectOption({ index: 1 })
    await page.getByRole('button', { name: 'Create key' }).click()

    const revealed = page.getByTestId('revealed-secret')
    await expect(revealed).toContainText('crmk_abc123-the-only-time-you-see-this')
    await expect(revealed).toContainText(/only time it is shown/)
    await expect.poll(() => stub.lastApiKey()?.['name']).toBe('Voice agent')

    // Dismissing it is final — there is no "show again" to offer.
    await page.getByRole('button', { name: /copied it/ }).click()
    await expect(revealed).toHaveCount(0)
  })

  test('the dialog explains what the template bounds', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: 'New key' }).click()
    await expect(page.getByText(/refused by name, not silently dropped/)).toBeVisible()
  })

  test('revoking marks the key rather than removing it', async ({ page }) => {
    await signIn(page)
    await page.getByTestId('api-key').first().getByRole('button', { name: 'Revoke' }).click()
    // The intake log references it — "which key posted this" survives.
    await expect(page.getByTestId('api-key').first()).toContainText('Revoked')
  })
})

test.describe('webhooks', () => {
  test('the event list comes from the server', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: 'New webhook' }).click()

    const events = page.getByRole('group', { name: 'Events' })
    // Offering a hand-copied list would let somebody subscribe to an event the
    // product never emits — silent forever.
    await expect(events.getByText('lead.stage_changed')).toBeVisible()
    await expect(events.getByRole('checkbox')).toHaveCount(8)
  })

  test('choosing no events means every event, and says so', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: 'New webhook' }).click()
    await expect(page.getByText(/will receive every event/)).toBeVisible()
  })

  test('creating a webhook sends the chosen events and reveals the secret', async ({ page }) => {
    const stub = await signIn(page)
    await page.getByRole('button', { name: 'New webhook' }).click()

    await page.getByLabel('Name').fill('Bolna bridge')
    await page.getByLabel('URL').fill('https://bridge.example.com/hook')
    await page.getByLabel('Permission template').selectOption({ index: 1 })
    await page.getByRole('group', { name: 'Events' }).getByText('lead.created').click()
    await page.getByRole('button', { name: 'Create webhook' }).click()

    await expect.poll(() => stub.lastWebhook()?.['name']).toBe('Bolna bridge')
    await expect.poll(() => stub.lastWebhook()?.['events']).toEqual(['lead.created'])
    await expect(page.getByTestId('revealed-secret')).toContainText('whsec_only-shown-once')
  })

  test('a test delivery reports what the endpoint answered', async ({ page }) => {
    await signIn(page)
    await page.getByTestId('webhook').first().getByRole('button', { name: 'Test' }).click()
    await expect(page.getByTestId('webhook-test-result')).toContainText(
      'your endpoint answered 200',
    )
  })

  test('a failed test says why', async ({ page }) => {
    await signIn(page, {
      webhookTest: {
        delivered: false,
        status_code: null,
        error: 'connection refused',
        signature: 'sha256=deadbeef',
      },
    })
    await page.getByTestId('webhook').first().getByRole('button', { name: 'Test' }).click()
    await expect(page.getByTestId('webhook-test-result')).toContainText('connection refused')
  })
})

test.describe('the outbound queue', () => {
  test('a dead event shows its error and offers a retry', async ({ page }) => {
    await signIn(page)
    const dead = page.getByTestId('outbox-event').first()
    await expect(dead).toContainText('lead.created')
    await expect(dead).toContainText('dead')
    await expect(dead).toContainText('connection refused')
    await expect(dead.getByRole('button', { name: 'Retry' })).toBeVisible()
  })

  test('a delivered event offers no retry', async ({ page }) => {
    await signIn(page)
    const delivered = page.getByTestId('outbox-event').nth(1)
    await expect(delivered).toContainText('delivered')
    await expect(delivered.getByRole('button', { name: 'Retry' })).toHaveCount(0)
  })

  test('retrying sends the event id', async ({ page }) => {
    const stub = await signIn(page)
    await page.getByTestId('outbox-event').first().getByRole('button', { name: 'Retry' }).click()
    await expect.poll(() => stub.retried()).toEqual(['outbox-1'])
  })

  test('the queue can be filtered to the failures', async ({ page }) => {
    await signIn(page)
    await page.getByLabel('Filter by delivery status').selectOption('DEAD')
    await expect(page.getByTestId('outbox-event')).toHaveCount(1)
    await expect(page.getByTestId('outbox-event').first()).toContainText('dead')
  })
})

test.describe('the intake log', () => {
  test('an accepted unknown field surfaces as a warning', async ({ page }) => {
    await signIn(page)
    // The API deliberately accepts it, so this screen is the only trace.
    await expect(page.getByTestId('intake-entry').first()).toContainText('utm_campaign')
    await expect(page.getByTestId('intake-entry').first()).toContainText('created')
  })

  test('rejections carry their reason', async ({ page }) => {
    await signIn(page)
    const rejected = page.getByTestId('intake-entry').nth(1)
    await expect(rejected).toContainText('rejected')
    await expect(rejected).toContainText("No stage called 'Nowhere'")
  })

  test('the log can be narrowed to rejections', async ({ page }) => {
    await signIn(page)
    await page.getByLabel('Rejections only').check()
    await expect(page.getByTestId('intake-entry')).toHaveCount(1)
    await expect(page.getByTestId('intake-entry').first()).toContainText('rejected')
  })
})

test('a refused request is not an empty integrations page', async ({ page }) => {
  await signIn(page, { integrationsAllowed: false })
  // "No keys yet" after a 403 reads as "you have no integrations", which is
  // the opposite of what happened.
  await expect(page.getByRole('alert').first()).toContainText(/permission template does not allow/)
  await expect(page.getByText('No keys yet')).toHaveCount(0)
})
