import { ReportsPage } from '../pages/ReportsPage'
import { HomePage } from '../features/home/HomePage'
import { WeeklyTargets } from '../features/management/WeeklyTargets'
import { LineTechSetup } from '../features/management/LineTechSetup'
import { ProductionStandards } from '../features/management/ProductionStandards'
import { Route, Routes } from 'react-router-dom'
import { HmiPage } from '../pages/HmiPage'
import { EngineeringPage } from '../pages/EngineeringPage'
import { ManagementPage } from '../pages/ManagementPage'
import { ManagementPerformancePage } from '../pages/ManagementPerformancePage'
import { ManagementActiveRunsPage } from '../pages/ManagementActiveRunsPage'
import { DashboardPage } from '../pages/DashboardPage'
import { DashboardEngineeringPage } from '../pages/DashboardEngineeringPage'
import { DashboardQaPage } from '../pages/DashboardQaPage'
import { DashboardIntelligencePage } from '../pages/DashboardIntelligencePage'
import { ManagementLayout, ManagementSessionRoot } from '../features/management/ManagementLayout'
import { RequireManagementSession } from '../features/management/RequireManagementSession'
import { DashboardLayout } from '../features/dashboard/shell/DashboardLayout'

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/hmi" element={<HmiPage />} />
      <Route path="/engineering" element={<EngineeringPage />} />
      {/* One shared Management session for every page below. */}
      <Route element={<ManagementSessionRoot />}>
        <Route path="/" element={<HomePage />} />
        <Route element={<ManagementLayout />}>
          <Route path="/management/reports" element={<RequireManagementSession><ReportsPage /></RequireManagementSession>} />
          <Route path="/management/weekly-targets" element={<RequireManagementSession><WeeklyTargets /></RequireManagementSession>} />
          <Route path="/management/linetech" element={<RequireManagementSession><LineTechSetup /></RequireManagementSession>} />
          <Route path="/management" element={<ManagementPage />} />
          <Route path="/management/performance" element={<ManagementPerformancePage />} />
          <Route path="/management/production-standards" element={<RequireManagementSession><ProductionStandards /></RequireManagementSession>} />
          <Route path="/management/active-runs" element={<ManagementActiveRunsPage />} />
        </Route>
        {/* The four management dashboard pages share one dark shell. */}
        <Route
          element={
            <RequireManagementSession>
              <DashboardLayout />
            </RequireManagementSession>
          }
        >
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/dashboard/engineering" element={<DashboardEngineeringPage />} />
          <Route path="/dashboard/qa" element={<DashboardQaPage />} />
          <Route path="/dashboard/operational-intelligence" element={<DashboardIntelligencePage />} />
        </Route>
      </Route>
    </Routes>
  )
}
