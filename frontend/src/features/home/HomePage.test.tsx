import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppRoutes } from '../../routes/AppRoutes'
import * as managementApi from '../management/api'
import { storeSession } from '../management/session/sessionStore'

vi.mock('../management/api')
vi.mock('../../components/ApiStatus', () => ({ ApiStatus: () => <span>API unavailable</span> }))
vi.mock('../../pages/HmiPage', () => ({ HmiPage: () => <h1>Existing HMI</h1> }))
vi.mock('../../pages/EngineeringPage', () => ({ EngineeringPage: () => <h1>Existing Engineering</h1> }))
vi.mock('../../pages/DashboardPage', () => ({ DashboardPage: () => <h1>Existing Production</h1> }))
vi.mock('../../pages/DashboardQaPage', () => ({ DashboardQaPage: () => <h1>Existing QA</h1> }))
vi.mock('../../pages/DashboardIntelligencePage', () => ({ DashboardIntelligencePage: () => <h1>Existing Intelligence</h1> }))

function History() { const location = useLocation(); const navigate = useNavigate(); return <><output data-testid="location">{location.pathname}</output><button onClick={() => navigate(-1)}>Browser back</button></> }
function open(path = '/') { return render(<MemoryRouter initialEntries={[path]}><AppRoutes /><History /></MemoryRouter>) }
function session() { storeSession({ token: 'test-token', managerName: 'Test Manager', expiresAt: '2099-01-01T00:00:00Z' }) }
beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear()
  vi.mocked(managementApi.logout).mockResolvedValue({ status: 'success', message: 'Signed out' })
  vi.mocked(managementApi.getSession).mockResolvedValue({ status: 'success', manager_name: 'Test Manager', expires_at: '2099-01-01T00:00:00Z' })
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})
afterEach(cleanup)

describe('Home navigation hub', () => {
  it('opens Home at the root and shares destinations across both card sections', () => {
    open()
    expect(screen.getByRole('heading', { name: 'Welcome to TonnageFlow Pulse' })).toBeInTheDocument()
    const quick = screen.getByRole('region', { name: 'Quick Access' })
    const all = screen.getByRole('region', { name: 'All Destinations' })
    for (const name of ['HMI', 'Engineering Workspace', 'Operational Intelligence']) {
      expect(within(quick).getByRole('link', { name }).getAttribute('href')).toBe(within(all).getByRole('link', { name }).getAttribute('href'))
    }
    expect(within(all).getByRole('link', { name: 'Performance' })).toHaveAttribute('href', '/management/performance')
    expect(document.querySelector('a button, button a')).toBeNull()
  })
  it.each([['HMI', 'Existing HMI'], ['Engineering Workspace', 'Existing Engineering']])('opens %s and supports browser back', (name, heading) => {
    open(); fireEvent.click(screen.getAllByRole('link', { name })[0])
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Browser back' }))
    expect(screen.getByRole('heading', { name: 'Welcome to TonnageFlow Pulse' })).toBeInTheDocument()
  })
  it.each(['Warehouse', 'Cleaning Plant', 'Settings'])('%s opens a notice without navigating', (name) => {
    open(); fireEvent.click(screen.getAllByRole('button', { name })[0])
    expect(screen.getByRole('dialog', { name })).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
  it('filters destinations without inventing routes', () => {
    open(); fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'quality' } })
    const all = screen.getByRole('region', { name: 'All Destinations' })
    expect(within(all).getByRole('link', { name: 'QA' })).toHaveAttribute('href', '/dashboard/qa')
    expect(within(all).queryByRole('link', { name: 'HMI' })).not.toBeInTheDocument()
  })
  it.each(['/dashboard', '/dashboard/qa', '/dashboard/operational-intelligence'])('keeps direct access to %s protected', async (path) => {
    open(path)
    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/management')
  })
  it('requires sign-in when a public visitor opens Production', async () => {
    open(); fireEvent.click(screen.getByRole('link', { name: 'Production' }))
    expect(await screen.findByRole('heading', { name: /management sign in/i })).toBeInTheDocument()
  })
  it('retains a validated manager session through Home, Production and back', async () => {
    session(); open()
    expect(await screen.findByText('Test Manager')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'Production' }))
    expect(await screen.findByRole('heading', { name: 'Existing Production' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Browser back' }))
    expect(screen.getByText('Test Manager')).toBeInTheDocument()
    expect(managementApi.logout).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByRole('link', { name: 'HMI' })[0])
    expect(managementApi.logout).toHaveBeenCalledWith('test-token')
    expect(sessionStorage.getItem('pulse.management.session.v1')).toBeNull()
  })
})
