import { ReportsPanel } from '../features/reports/ReportsPanel'
import { useManagementSession } from '../features/management/session/useManagementSession'
export function ReportsPage() { const { session } = useManagementSession(); return <ReportsPanel token={session?.token} initialType="management" /> }
