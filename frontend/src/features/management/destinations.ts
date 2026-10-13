/** Protected pages that send a signed-out user to the Management login
 * and back again afterwards. Only these paths are ever honoured as a
 * post-login destination - never an arbitrary value from history state. */
export const PROTECTED_DESTINATIONS: Readonly<Record<string, string>> = {
  '/management/reports': 'Shift reports',
  '/management/weekly-targets': 'Weekly targets',
  '/management/linetech': 'LineTech setup',
  '/management/performance': 'Technician performance',
  '/management/active-runs': 'Active runs',
  '/dashboard': 'Production dashboard',
  '/dashboard/engineering': 'Engineering dashboard',
  '/dashboard/qa': 'QA dashboard',
  '/dashboard/operational-intelligence': 'Operational Intelligence',
}

export interface ProtectedDestination {
  path: string
  label: string
}

export function protectedDestinationFrom(state: unknown): ProtectedDestination | null {
  if (!state || typeof state !== 'object' || !('from' in state)) return null
  const from = (state as { from: unknown }).from
  if (typeof from !== 'string') return null
  const path = from.split('?')[0]
  if (!Object.hasOwn(PROTECTED_DESTINATIONS, path)) return null
  return { path: from, label: PROTECTED_DESTINATIONS[path] }
}

/** "14:32" in UK time - the same clock the HMI shows. */
export function formatSessionEnd(expiresAt: string): string | null {
  const date = new Date(expiresAt)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/London',
  })
}
