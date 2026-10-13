import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppRoutes } from '../../routes/AppRoutes'
import * as managementApi from './api'
import * as dashboardApi from '../dashboard/api'
import * as performanceApi from '../performance/api'
import { ApiRequestError, apiClient } from '../../api/client'

vi.mock('./api')
// This file is about the session, not the data: the protected pages'
// own reads stay pending (their states are covered in their own tests).
vi.mock('../dashboard/api')
vi.mock('../performance/api')

// AppShell's ApiStatus (and the HMI, when a test navigates there) call
// apiClient.get directly - stub only that method so it never resolves,
// keeping ApiRequestError as the real class.
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    apiClient: { ...actual.apiClient, get: vi.fn(() => new Promise(() => {})) },
  }
})

const LOGIN_SUCCESS = {
  status: 'success',
  token: 'mgmt-test-token-123',
  manager_name: 'Kuri',
  // Far future: only the expiry tests use a real deadline.
  expires_at: '2099-01-01T00:00:00+00:00',
}

beforeEach(() => {
  vi.mocked(managementApi.logout).mockResolvedValue({ status: 'success', message: 'Logged out.' })
  vi.mocked(dashboardApi.getLive).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.getOverview).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.getFilterOptions).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.getHourly).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.getLineDecisions).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.getLineStops).mockReturnValue(new Promise(() => {}))
  vi.mocked(dashboardApi.getWeeklyTargets).mockReturnValue(new Promise(() => {}))
  vi.mocked(performanceApi.getTechnicianPerformance).mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  // Unmount first: leaving the Management area revokes a live session,
  // and that logout call must land on this test's mock, not the next's.
  cleanup()
  vi.useRealTimers()
  vi.mocked(managementApi.login).mockReset()
  vi.mocked(managementApi.logout).mockReset()
  vi.mocked(managementApi.getSession).mockReset()
  vi.mocked(apiClient.get).mockClear()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  )
}

function fillAndSubmit(name = 'Kuri', pin = '1234') {
  fireEvent.change(screen.getByLabelText(/your name/i), { target: { value: name } })
  fireEvent.change(screen.getByLabelText(/management pin/i), { target: { value: pin } })
  fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
}

async function signInAtManagement() {
  vi.mocked(managementApi.login).mockResolvedValue(LOGIN_SUCCESS)
  renderAt('/management')
  fillAndSubmit()
  await screen.findByRole('heading', { name: /^management$/i })
}

function nav() {
  return within(screen.getByRole('navigation', { name: /primary/i }))
}

describe('Management sign-in', () => {
  it('shows a name field, a masked PIN field and a disabled Sign In until both are filled', () => {
    renderAt('/management')

    expect(screen.getByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/management pin/i)).toHaveAttribute('type', 'password')

    const submit = screen.getByRole('button', { name: /^sign in$/i })
    expect(submit).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/your name/i), { target: { value: '   ' } })
    fireEvent.change(screen.getByLabelText(/management pin/i), { target: { value: '1234' } })
    expect(submit).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/your name/i), { target: { value: 'Kuri' } })
    expect(submit).toBeEnabled()
  })

  it('signs in, sends the trimmed name, and shows the Management home with its seven areas', async () => {
    vi.mocked(managementApi.login).mockResolvedValue(LOGIN_SUCCESS)
    renderAt('/management')

    fillAndSubmit('  Kuri  ', '1234')

    expect(await screen.findByRole('heading', { name: /^management$/i })).toBeInTheDocument()
    expect(managementApi.login).toHaveBeenCalledTimes(1)
    expect(managementApi.login).toHaveBeenCalledWith({ pin: '1234', manager_name: 'Kuri' })

    const session = screen.getByRole('region', { name: /management session/i })
    expect(within(session).getByText('Kuri')).toBeInTheDocument()
    expect(within(session).getByRole('button', { name: /log out/i })).toBeInTheDocument()

    for (const title of [
      'Factory setup',
      'Active runs',
      'Weekly targets',
      'Technician performance',
      'Production dashboard',
    ]) {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument()
    }
    const cards = screen.getAllByRole('listitem')
    expect(cards).toHaveLength(7)

    // Working areas: visibly "Open" and real links to real routes.
    expect(screen.getByRole('link', { name: /production dashboard/i })).toHaveAttribute('href', '/dashboard')
    expect(screen.getByRole('link', { name: /technician performance/i })).toHaveAttribute(
      'href',
      '/management/performance',
    )
    expect(screen.getByRole('link', { name: /active runs/i })).toHaveAttribute('href', '/management/active-runs')
    expect(screen.getAllByText('Open', { exact: true })).toHaveLength(7)
    expect(screen.getByRole('link', {name:/factory setup/i})).toHaveAttribute('href','/management/linetech')
    expect(screen.getByRole('link', {name:/production standards/i})).toHaveAttribute('href','/management/production-standards')

    expect(screen.getByRole('link', {name:/weekly targets/i})).toHaveAttribute('href','/management/weekly-targets')
    expect(screen.queryByText('Next stage')).not.toBeInTheDocument()
  })

  it('keeps only token, name and expiry in this tab - never the PIN, localStorage or a cookie', async () => {
    await signInAtManagement()

    expect(window.localStorage.length).toBe(0)
    expect(document.cookie).toBe('')
    expect(window.sessionStorage.length).toBe(1)
    const stored = window.sessionStorage.getItem('pulse.management.session.v1') ?? ''
    expect(JSON.parse(stored)).toEqual({
      token: 'mgmt-test-token-123',
      managerName: 'Kuri',
      expiresAt: '2099-01-01T00:00:00+00:00',
    })
    expect(stored).not.toContain('1234')
  })

  it('shows a safe message for an incorrect PIN, clears the PIN and keeps the name', async () => {
    vi.mocked(managementApi.login).mockRejectedValue(new ApiRequestError(401, 'Incorrect PIN.'))
    renderAt('/management')

    fillAndSubmit('Kuri', '9999')

    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect PIN. Please try again.')
    await waitFor(() => expect(screen.getByLabelText(/management pin/i)).toHaveValue(''))
    expect(screen.getByLabelText(/your name/i)).toHaveValue('Kuri')
    expect(screen.getByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /management session/i })).not.toBeInTheDocument()
  })

  it.each([
    [429, 'Too many failed attempts.', /too many attempts\. please wait/i],
    [503, 'Management login is not configured.', /not set up on the server/i],
    [422, 'Manager name cannot be blank.', /enter your name and the management pin/i],
    [500, 'boom', /the pulse server had a problem/i],
    [0, 'Could not reach the API.', /could not reach the pulse server/i],
  ])('shows a safe message for HTTP %s', async (status, detail, expected) => {
    vi.mocked(managementApi.login).mockRejectedValue(new ApiRequestError(status, detail))
    renderAt('/management')

    fillAndSubmit()

    expect(await screen.findByRole('alert')).toHaveTextContent(expected)
    expect(screen.getByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
  })

  it('shows a generic message for an unexpected (non-API) error', async () => {
    vi.mocked(managementApi.login).mockRejectedValue(new TypeError('unexpected'))
    renderAt('/management')

    fillAndSubmit()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not sign in. Please try again.')
  })
})

describe('Shared session across protected routes', () => {
  it('reuses one sign-in for /management, /management/performance and /dashboard', async () => {
    await signInAtManagement()

    fireEvent.click(nav().getByRole('link', { name: 'Dashboard' }))
    expect(await screen.findByRole('heading', { level: 1, name: /^live operations dashboard$/i })).toBeInTheDocument()

    fireEvent.click(nav().getByRole('link', { name: 'Performance' }))
    expect(
      await screen.findByRole('heading', { level: 1, name: /^line technician performance$/i }),
    ).toBeInTheDocument()
    // Only the page you are on is highlighted - not Management as well.
    expect(nav().getByRole('link', { name: 'Performance' })).toHaveClass('app-shell__nav-link--active')
    expect(nav().getByRole('link', { name: 'Management' })).not.toHaveClass('app-shell__nav-link--active')

    fireEvent.click(nav().getByRole('link', { name: 'Management' }))
    expect(await screen.findByRole('heading', { name: /^management$/i })).toBeInTheDocument()
    expect(nav().getByRole('link', { name: 'Management' })).toHaveClass('app-shell__nav-link--active')
    expect(nav().getByRole('link', { name: 'Performance' })).not.toHaveClass('app-shell__nav-link--active')

    expect(managementApi.login).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('region', { name: /management session/i })).toBeInTheDocument()
  })

  it('opens the protected pages from the Management home cards', async () => {
    await signInAtManagement()

    fireEvent.click(screen.getByRole('link', { name: /production dashboard/i }))
    expect(await screen.findByRole('heading', { level: 1, name: /^live operations dashboard$/i })).toBeInTheDocument()
  })
})

describe('Protected-route redirect', () => {
  it.each([
    ['/dashboard', /sign in to open production dashboard/i, /^live operations dashboard$/i],
    [
      '/management/performance',
      /sign in to open technician performance/i,
      /^line technician performance$/i,
    ],
  ])('sends a signed-out user from %s to sign-in, then back there', async (path, notice, heading) => {
    vi.mocked(managementApi.login).mockResolvedValue(LOGIN_SUCCESS)
    renderAt(path)

    expect(screen.getByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByText(notice)).toBeInTheDocument()

    fillAndSubmit()

    // Level 1 = the page's own heading, never the Management home's
    // "Technician performance" card (level 2) - the home is skipped.
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1, name: /^management$/i })).not.toBeInTheDocument()
  })

  it('stays on the Management home after a direct sign-in at /management', async () => {
    await signInAtManagement()

    expect(screen.queryByRole('heading', { level: 1, name: /^live operations dashboard$/i })).not.toBeInTheDocument()
  })
})

describe('Session expiry', () => {
  it('returns to sign-in automatically when the session reaches expires_at', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-09-25T10:00:00Z'))
    vi.mocked(managementApi.login).mockResolvedValue({
      ...LOGIN_SUCCESS,
      expires_at: '2026-09-25T10:30:00+00:00',
    })
    renderAt('/dashboard')
    fillAndSubmit()
    expect(await screen.findByRole('heading', { level: 1, name: /^live operations dashboard$/i })).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(29 * 60 * 1000)
    })
    expect(screen.getByRole('heading', { level: 1, name: /^live operations dashboard$/i })).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(60 * 1000)
    })

    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByText(/your management session has expired/i)).toBeInTheDocument()
    // Still remembers where the manager was, so signing in returns there.
    expect(screen.getByText(/sign in to open production dashboard/i)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /management session/i })).not.toBeInTheDocument()
  })

  it('treats an already-past expires_at as expired straight away', async () => {
    vi.mocked(managementApi.login).mockResolvedValue({
      ...LOGIN_SUCCESS,
      expires_at: '2000-01-01T00:00:00+00:00',
    })
    renderAt('/management')
    fillAndSubmit()

    expect(await screen.findByText(/your management session has expired/i)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
  })
})

describe('Logout', () => {
  it('revokes the session, returns to sign-in and protects the pages again', async () => {
    await signInAtManagement()

    fireEvent.click(screen.getByRole('button', { name: /log out/i }))

    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByText(/you have signed out of management/i)).toBeInTheDocument()
    expect(managementApi.logout).toHaveBeenCalledTimes(1)
    expect(managementApi.logout).toHaveBeenCalledWith('mgmt-test-token-123')

    fireEvent.click(nav().getByRole('link', { name: 'Dashboard' }))
    expect(await screen.findByText(/sign in to open production dashboard/i)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1, name: /^live operations dashboard$/i })).not.toBeInTheDocument()
  })

  it('logs out from a protected page and lands on the plain sign-in screen', async () => {
    await signInAtManagement()
    fireEvent.click(nav().getByRole('link', { name: 'Dashboard' }))
    await screen.findByRole('heading', { level: 1, name: /^live operations dashboard$/i })

    fireEvent.click(screen.getByRole('button', { name: /log out/i }))

    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.queryByText(/sign in to open/i)).not.toBeInTheDocument()
  })

  it('still signs out locally when the logout request fails', async () => {
    await signInAtManagement()
    vi.mocked(managementApi.logout).mockRejectedValue(new ApiRequestError(0, 'offline'))

    fireEvent.click(screen.getByRole('button', { name: /log out/i }))

    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /management session/i })).not.toBeInTheDocument()
  })

  it('ends and revokes the session when leaving the Management area', async () => {
    await signInAtManagement()

    fireEvent.click(nav().getByRole('link', { name: 'HMI' }))
    await waitFor(() => expect(managementApi.logout).toHaveBeenCalledWith('mgmt-test-token-123'))

    fireEvent.click(nav().getByRole('link', { name: 'Management' }))
    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
  })

  it('clears the stored session on Log Out', async () => {
    await signInAtManagement()
    fireEvent.click(screen.getByRole('button', { name: /log out/i }))

    await screen.findByRole('heading', { name: /management sign in/i })
    expect(window.sessionStorage.length).toBe(0)
  })

  it('clears the stored session when leaving the Management area', async () => {
    await signInAtManagement()
    fireEvent.click(nav().getByRole('link', { name: 'HMI' }))

    await waitFor(() => expect(managementApi.logout).toHaveBeenCalled())
    expect(window.sessionStorage.length).toBe(0)
  })
})

/** A browser refresh: the stored tab session exists, React state does not. */
function storeTabSession(expiresAt = '2099-01-01T00:00:00+00:00') {
  window.sessionStorage.setItem(
    'pulse.management.session.v1',
    JSON.stringify({ token: 'mgmt-test-token-123', managerName: 'Kuri', expiresAt }),
  )
}

describe('Browser refresh', () => {
  it('stays signed in on the dashboard once the server confirms the kept session', async () => {
    storeTabSession()
    vi.mocked(managementApi.getSession).mockResolvedValue({
      status: 'success',
      manager_name: 'Kuri',
      expires_at: '2099-01-01T00:00:00+00:00',
    })

    renderAt('/dashboard')

    // Checking first - never a flash of the sign-in screen.
    expect(screen.getByText(/checking your management session/i)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /management sign in/i })).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { level: 1, name: /^live operations dashboard$/i })).toBeInTheDocument()
    expect(managementApi.getSession).toHaveBeenCalledWith('mgmt-test-token-123', expect.any(AbortSignal))
    expect(managementApi.login).not.toHaveBeenCalled()
  })

  it('returns to sign-in when the server no longer knows the token (restart, revoked or expired)', async () => {
    storeTabSession()
    vi.mocked(managementApi.getSession).mockRejectedValue(
      new ApiRequestError(401, 'Management session is invalid or has expired.'),
    )

    renderAt('/dashboard')

    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByText(/your management session has expired/i)).toBeInTheDocument()
    expect(screen.getByText(/sign in to open production dashboard/i)).toBeInTheDocument()
    expect(window.sessionStorage.length).toBe(0)
  })

  it('does not trust a kept session it could not check', async () => {
    storeTabSession()
    vi.mocked(managementApi.getSession).mockRejectedValue(new ApiRequestError(0, 'Network error'))

    renderAt('/management')

    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByText(/could not check your management session/i)).toBeInTheDocument()
    expect(window.sessionStorage.length).toBe(0)
  })

  it('ignores a kept session whose expiry has passed without asking the server', async () => {
    storeTabSession('2000-01-01T00:00:00+00:00')

    renderAt('/management')

    expect(screen.getByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(managementApi.getSession).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
  })
})
