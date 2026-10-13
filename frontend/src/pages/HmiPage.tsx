import { ReportLauncher } from '../features/reports/ReportsPanel'
import { TaskCapture } from '../features/hmi/TaskCapture'
import { DeviceGate } from '../features/hmi/DeviceGate'
import { AppShell } from '../layouts/AppShell'
import { HmiScreen } from '../features/hmi/HmiScreen'

export function HmiPage() {
  return (
    <AppShell>
      <DeviceGate><ReportLauncher listenForShift /><TaskCapture /><HmiScreen /></DeviceGate>
    </AppShell>
  )
}
