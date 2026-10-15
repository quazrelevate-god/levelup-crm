import { expect, test, type Page } from '@playwright/test'

import {
  action,
  callLeadRef,
  stubApi,
  TIMELINE,
  voiceCall,
  voiceCallDetail,
  WORKSPACE_A,
  WORKSPACE_B,
  type StubOptions,
} from './fixtures/api'

/**
 * The AI Call Summary card and the Call Details module (docs/13).
 *
 * The assertion that matters most is the one about *which* call: a lead with
 * three calls must show the newest completed one, and the "View Raw Call
 * Details" link must carry that call's own CRM id. Navigating by lead name or
 * phone number is what this feature exists to avoid.
 *
 * The second is isolation: raw call data belongs on the call's own page and
 * must never be dumped into the lead panel.
 */

const NEWEST_SUMMARY = 'Perumal confirmed his interest in Breakthrough Filmmaking.'
const OLDEST_SUMMARY = 'An older conversation that must not be shown on the card.'

const OTHER_LEAD = callLeadRef({
  lead_id: 'lead-2',
  identity_value: 'Другой',
  primary_h1: 'Another Lead',
  primary_h2: 'Video Editing Academy',
})

/** Three calls for lead-1 (newest first) plus one for a different lead. */
const CALLS = [
  voiceCall({ id: 'call-newest', summary: NEWEST_SUMMARY, completed_at: '2026-09-25T10:00:00Z' }),
  voiceCall({
    id: 'call-middle',
    execution_id: 'exec-middle',
    summary: 'The middle call.',
    duration_seconds: 130,
    completed_at: '2026-09-22T10:00:00Z',
  }),
  voiceCall({
    id: 'call-oldest',
    execution_id: 'exec-oldest',
    summary: OLDEST_SUMMARY,
    duration_seconds: 60,
    completed_at: '2026-09-20T10:00:00Z',
  }),
  voiceCall({
    id: 'call-other-lead',
    lead: OTHER_LEAD,
    execution_id: 'exec-other',
    summary: "Another lead's conversation entirely.",
    duration_seconds: 133,
    completed_at: '2026-09-24T10:00:00Z',
  }),
]

const DETAILS = {
  'call-newest': voiceCallDetail({ id: 'call-newest', summary: NEWEST_SUMMARY }),
  'call-other-lead': voiceCallDetail({
    id: 'call-other-lead',
    lead: OTHER_LEAD,
    execution_id: 'exec-other',
    summary: "Another lead's conversation entirely.",
    transcript: 'assistant: a different call',
  }),
}

const WITH_CALLS: StubOptions = { voiceCalls: CALLS, voiceCallDetails: DETAILS }

async function signIn(page: Page, options: StubOptions = {}) {
  const stub = await stubApi(page, options)
  await page.goto('/login')
  await page.getByLabel('Email').fill('owner@example.com')
  await page.getByLabel('Password').fill('correct-horse-battery-staple')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Leads' })).toBeVisible()
  return stub
}

async function openLead(page: Page) {
  await page.getByTestId('lead-row').first().click()
  await expect(page.getByTestId('lead-detail')).toBeVisible()
}

// --- the lead panel's card ----------------------------------------------------

test.describe('AI Call Summary card', () => {
  test('shows the most recent completed call for this lead', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await openLead(page)

    const card = page.getByTestId('ai-call-summary-card')
    await expect(card).toBeVisible()
    await expect(card.getByText('AI Call Summary')).toBeVisible()
    await expect(card.getByText('Call placed by AI')).toBeVisible()
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(NEWEST_SUMMARY)

    // Not an older call, and not another lead's.
    await expect(card).not.toContainText(OLDEST_SUMMARY)
    await expect(card).not.toContainText("Another lead's conversation entirely.")

    // Duration and status, formatted for people.
    await expect(card).toContainText('Duration: 1m 57s')
    await expect(card).toContainText('Status: Completed')
  })

  test('sits inside Log activity and keeps raw data out of the lead panel', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await openLead(page)

    // The card lives in the existing section, after the AI Call button.
    const panel = page.getByTestId('lead-detail')
    await expect(panel.getByRole('heading', { name: 'Log activity' })).toBeVisible()
    await expect(panel.getByRole('button', { name: 'AI Call' })).toBeVisible()
    await expect(panel.getByTestId('ai-call-summary-card')).toBeVisible()
    // And the sections around it are untouched.
    await expect(panel.getByRole('heading', { name: 'Timeline' })).toBeVisible()
    await expect(panel.getByPlaceholder('Add a note…')).toBeVisible()

    // Raw call data belongs only on the call page.
    await expect(panel).not.toContainText('Raw provider payload')
    await expect(panel).not.toContainText('assistant: Hello Perumal')
    await expect(panel.getByTestId('call-raw-payload')).toHaveCount(0)
    await expect(panel.getByTestId('call-transcript')).toHaveCount(0)
  })

  test('says so plainly when the lead has no completed AI call', async ({ page }) => {
    await signIn(page, { voiceCalls: [] })
    await openLead(page)

    const card = page.getByTestId('ai-call-summary-card')
    await expect(card).toContainText('No completed AI call yet.')
    await expect(page.getByTestId('view-raw-call-details')).toHaveCount(0)
  })

  test('the existing timeline entry is still there', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await openLead(page)

    // The card is additional information, not a replacement: the timeline
    // keeps its own entries, and one call does not become two records.
    await expect(page.getByTestId('lead-timeline')).toBeVisible()
    await expect(page.getByTestId('timeline-entry').first()).toBeVisible()
  })
})

// --- the bridge ----------------------------------------------------------------

test.describe('View Raw Call Details', () => {
  test('opens that exact call by its CRM call id', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await openLead(page)

    const link = page.getByTestId('view-raw-call-details')
    await expect(link).toBeVisible()
    // By id — never by lead name or phone number.
    await expect(link).toHaveAttribute('href', '/calls/call-newest')

    await link.click()
    await expect(page).toHaveURL(/\/calls\/call-newest$/)
    await expect(page.getByTestId('call-detail')).toBeVisible()
    await expect(page.getByTestId('call-summary')).toHaveText(NEWEST_SUMMARY)
  })
})

// --- the module ------------------------------------------------------------------

test.describe('Call Details module', () => {
  test('is reachable from the main navigation', async ({ page }) => {
    await signIn(page, WITH_CALLS)

    await page.getByRole('link', { name: 'Call details' }).click()
    await expect(page).toHaveURL(/\/calls$/)
    await expect(page.getByRole('heading', { name: 'Call details' })).toBeVisible()
  })

  test('lists every call, newest first, with who and how it went', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await page.goto('/calls')

    const rows = page.getByTestId('call-row')
    await expect(rows).toHaveCount(4)

    const first = rows.first()
    await expect(first).toContainText('Perumal')
    await expect(first).toContainText('Breakthrough Filmmaking')
    await expect(first).toContainText('1m 57s')
    await expect(first).toContainText('Completed')

    // Both leads appear here — this is the workspace's list, not one lead's.
    await expect(rows.nth(3)).toContainText('Another Lead')
    await expect(rows.nth(3)).toContainText('Video Editing Academy')
  })

  test('opens one call from the list', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await page.goto('/calls')

    await page.getByTestId('call-row').first().getByTestId('view-call-details').click()
    await expect(page).toHaveURL(/\/calls\/call-newest$/)
  })

  test('says so when there are no calls at all', async ({ page }) => {
    await signIn(page, { voiceCalls: [] })
    await page.goto('/calls')
    await expect(page.getByText('No AI calls have been recorded yet.')).toBeVisible()
  })
})

// --- one call's page ---------------------------------------------------------------

test.describe('the call page', () => {
  test('shows this call and only this call', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await page.goto('/calls/call-newest')

    const detail = page.getByTestId('call-detail')
    await expect(detail.getByRole('heading', { name: 'Perumal' })).toBeVisible()
    await expect(detail).toContainText('Breakthrough Filmmaking')
    await expect(detail).toContainText('Status: Completed')
    await expect(detail).toContainText('Duration: 1m 57s')
    await expect(page.getByTestId('call-summary')).toHaveText(NEWEST_SUMMARY)
    // Nothing from the other lead's call.
    await expect(detail).not.toContainText("Another lead's conversation entirely.")
  })

  test('the heavy sections expand and collapse', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await page.goto('/calls/call-newest')

    // Collapsed by default: the content is not visible until asked for.
    await expect(page.getByTestId('call-transcript')).toBeHidden()
    await expect(page.getByTestId('call-extracted-data')).toBeHidden()
    await expect(page.getByTestId('call-raw-payload')).toBeHidden()

    await page.getByTestId('section-call-information').getByText('Call information').click()
    await expect(page.getByText('CRM call ID')).toBeVisible()
    await expect(page.getByText('Bolna execution ID')).toBeVisible()

    await page.getByTestId('section-transcript').getByText('Transcript').click()
    await expect(page.getByTestId('call-transcript')).toContainText('assistant: Hello Perumal.')

    await page.getByTestId('section-extracted-data').getByText('Extracted data').click()
    const extracted = page.getByTestId('call-extracted-data')
    // Grouped extractions read as "General · Call Summary" with the value
    // itself, not as a wall of nested JSON.
    await expect(extracted).toContainText('General · Call Summary')
    await expect(extracted).toContainText('Confirmed interest.')
    // The original nesting is still available behind "Show as JSON".
    await expect(page.getByTestId('call-extracted-json')).toBeHidden()
    await extracted.getByText('Show as JSON').click()
    await expect(page.getByTestId('call-extracted-json')).toContainText('subjective')

    await page.getByTestId('section-raw-payload').getByText('Raw provider payload').click()
    const raw = page.getByTestId('call-raw-payload')
    await expect(raw).toContainText('telephony_data')
    // Whatever the server redacted stays redacted on screen.
    await expect(raw).toContainText('<redacted>')

    // And collapsing hides it again.
    await page.getByTestId('section-transcript').getByText('Transcript').click()
    await expect(page.getByTestId('call-transcript')).toBeHidden()
  })

  test('a call with no extractions says so', async ({ page }) => {
    await signIn(page, {
      ...WITH_CALLS,
      voiceCallDetails: {
        'call-newest': voiceCallDetail({ extracted_data: {}, extractions: [] }),
      },
    })
    await page.goto('/calls/call-newest')

    await page.getByTestId('section-extracted-data').getByText('Extracted data').click()
    await expect(page.getByText('Nothing was extracted from this call.')).toBeVisible()
    // The rest of the call is unaffected.
    await expect(page.getByTestId('call-summary')).toBeVisible()
  })

  test('an unknown call id reads as a message, not an API error', async ({ page }) => {
    await signIn(page, WITH_CALLS)
    await page.goto('/calls/does-not-exist')

    await expect(page.getByTestId('call-not-found')).toHaveText('Call details not found.')
    await expect(page.getByText('not_found')).toHaveCount(0)
  })

  test('a template without call history is told, not shown a stack trace', async ({ page }) => {
    await signIn(page, { ...WITH_CALLS, callHistoryAllowed: false })
    await page.goto('/calls')

    await expect(page.getByText(/permission template may not allow/)).toBeVisible()
  })
})

// --- the summary arriving without a page reload --------------------------------
//
// Bolna delivers twice: the hangup first (transcript, no summary) and the
// completed result seconds later, which upgrades the same row and the same
// timeline action in place. The panel used to settle on the recorded fallback
// and only show the real summary after a manual refresh. These pin both halves
// of the fix — the card's query is invalidated, and the poll waits for a
// summary rather than for the log's mere presence.

/** Mirrors LeadDetail's own interval; the panel does not export it. */
const POLL_MS = 5_000

const FALLBACK_SUMMARY = 'AI call ended without a conversation'
const UPGRADED_SUMMARY = 'Ada asked about the schedule and the fee.'

/** The hangup delivery: logged, transcript only, no vendor summary yet.
 *
 * `performed_at` is *now*, not the suite's fixed date, because this action
 * represents a call that has only just hung up — which is the whole premise of
 * these tests. The panel decides whether a summary could still be coming from
 * how old the log is, so a fixture dated weeks back would describe a call
 * nobody is waiting on and the polling tests would be testing nothing.
 */
const FALLBACK_TIMELINE = [
  action({
    id: 'action-ai-call',
    kind: 'CALL_LOGGED',
    body: FALLBACK_SUMMARY,
    performed_at: new Date().toISOString(),
    payload: {
      execution_id: 'exec-pending',
      source: 'AI_CALL',
      summary_source: 'FALLBACK',
      has_transcript: true,
    },
  }),
  ...TIMELINE,
]

const PENDING_CALL = voiceCall({
  id: 'call-triggered',
  execution_id: 'exec-pending',
  summary: FALLBACK_SUMMARY,
  summary_source: 'FALLBACK',
  completed_at: '2026-09-26T10:00:00Z',
})

const AWAITING: StubOptions = {
  voiceCalls: [PENDING_CALL],
  voiceCallDetails: { 'call-triggered': voiceCallDetail({ id: 'call-triggered' }) },
  timeline: FALLBACK_TIMELINE,
}

/** Start an AI call from the open lead panel, so the panel begins polling. */
async function startAiCall(page: Page) {
  await page.getByRole('button', { name: '🤖 AI Call' }).click()
  await expect(page.getByText(/AI call started/)).toBeVisible()
}

test.describe('the AI summary appears without a manual refresh', () => {
  test('the upgraded summary replaces the fallback in the same mounted panel', async ({ page }) => {
    const stub = await signIn(page, AWAITING)
    await openLead(page)

    // The fallback is what the hangup delivery left behind.
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(FALLBACK_SUMMARY)
    await startAiCall(page)

    // Bolna's completed delivery lands. Nothing remounts and nothing is
    // reloaded — the card must pick this up on its own.
    stub.completeAiCall(UPGRADED_SUMMARY)

    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(UPGRADED_SUMMARY, {
      timeout: 20_000,
    })
    await expect(page.getByTestId('lead-detail')).toBeVisible()
    await expect(page.getByText('AI call finished · summary added to the timeline')).toBeVisible()
  })

  test('the card re-reads its own list query, not just the timeline', async ({ page }) => {
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    await startAiCall(page)

    // Recorded as "METHOD /path", so a GET of the list is distinguishable
    // from the POST that started the call.
    const callReads = () =>
      stub.requests.filter((entry) => entry.startsWith('GET ') && entry.endsWith('/voice/calls'))
        .length
    const before = callReads()
    stub.completeAiCall(UPGRADED_SUMMARY)
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(UPGRADED_SUMMARY, {
      timeout: 20_000,
    })

    // Invalidating the calls key is the whole fix: without it the card keeps
    // its first answer and this count never moves.
    expect(callReads()).toBeGreaterThan(before)
  })

  test('polling continues while the summary is still the fallback', async ({ page }) => {
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    await startAiCall(page)

    // The log is already on the timeline, so presence alone would have stopped
    // the poll here. It must keep going: the timeline is read more than once.
    const timelineReads = () => stub.requests.filter((path) => path.includes('/actions')).length
    const first = timelineReads()
    await expect.poll(timelineReads, { timeout: 20_000 }).toBeGreaterThan(first)

    // And the panel has not declared the call finished.
    await expect(page.getByText('AI call finished · summary added to the timeline')).toHaveCount(0)
  })

  test('a panel that never started the call still picks the summary up', async ({ page }) => {
    // The reload case, and the one that bit in production on 2026-10-15. The
    // page was reloaded while Bolna was still summarising: `awaitingExecution`
    // is component state, so it came back null, polling never restarted, and
    // the upgrade arrived six seconds after the last read — invisible until
    // someone refreshed by hand.
    //
    // So no `startAiCall` here. The panel is mounted cold onto a lead whose
    // call is already logged with the fallback, exactly as it is after a
    // reload, and it must resume polling from the data alone.
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(FALLBACK_SUMMARY)

    stub.completeAiCall(UPGRADED_SUMMARY)

    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(UPGRADED_SUMMARY, {
      timeout: 20_000,
    })
  })

  test('a cold panel stops polling once the summary has landed', async ({ page }) => {
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    stub.completeAiCall(UPGRADED_SUMMARY)
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(UPGRADED_SUMMARY, {
      timeout: 20_000,
    })

    // Derived polling must settle on its own, or a reloaded tab would read the
    // timeline every five seconds for as long as it stayed open.
    const settled = stub.requests.filter((path) => path.includes('/actions')).length
    await page.waitForTimeout(POLL_MS * 2)
    expect(stub.requests.filter((path) => path.includes('/actions')).length).toBe(settled)
  })

  test('polling stops once the summary is no longer the fallback', async ({ page }) => {
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    await startAiCall(page)
    stub.completeAiCall(UPGRADED_SUMMARY)
    await expect(page.getByText('AI call finished · summary added to the timeline')).toBeVisible({
      timeout: 20_000,
    })

    // Settled: the timeline stops being re-read.
    const settled = stub.requests.filter((path) => path.includes('/actions')).length
    await page.waitForTimeout(POLL_MS * 2)
    expect(stub.requests.filter((path) => path.includes('/actions')).length).toBe(settled)
  })

  test('a lead whose call never completes falls back to the bounded notice', async ({ page }) => {
    // The 15-minute bound is not wall-clock-testable in an e2e run; what is
    // testable is that nothing claims success while the summary is a fallback,
    // and that the panel keeps working rather than spinning on an error.
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    await startAiCall(page)

    await page.waitForTimeout(POLL_MS * 2)
    await expect(page.getByText('AI call finished · summary added to the timeline')).toHaveCount(0)
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(FALLBACK_SUMMARY)
    await expect(page.getByTestId('lead-detail')).toBeVisible()
    expect(stub.requests.filter((path) => path.includes('/actions')).length).toBeGreaterThan(1)
  })

  test("one workspace's refresh never reads another workspace's calls", async ({ page }) => {
    const stub = await signIn(page, AWAITING)
    await openLead(page)
    await startAiCall(page)
    stub.completeAiCall(UPGRADED_SUMMARY)
    await expect(page.getByTestId('ai-call-summary-text')).toHaveText(UPGRADED_SUMMARY, {
      timeout: 20_000,
    })

    // Every call read this session is scoped to the signed-in workspace. The
    // other workspace is never touched, and the card's own query carries this
    // lead's id — which the stub enforces by filtering on it server-side, so a
    // leak would have shown another lead's summary in the assertion above.
    const reads = stub.requests.filter((entry) => entry.includes('/voice/calls'))
    expect(reads.length).toBeGreaterThan(0)
    for (const entry of reads) {
      expect(entry).toContain(`/workspaces/${WORKSPACE_A}/`)
      expect(entry).not.toContain(WORKSPACE_B)
    }
  })
})
