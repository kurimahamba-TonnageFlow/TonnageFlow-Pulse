export type Destination = {
  id: string; title: string; description: string; tone: string; icon: string;
  to?: string; badge?: string; notice?: string;
}

export const DESTINATIONS: readonly Destination[] = [
  { id: 'warehouse', title: 'Warehouse', description: 'Track intake, film storage and consumables.', tone: 'blue', icon: 'warehouse', badge: 'Phase 2', notice: 'Planned for Phase 2: consumables intake, film and packaging storage, stock movements and inventory. This area is not available yet.' },
  { id: 'production', title: 'Production', description: 'Merge points for runs, output and production flow.', tone: 'green', icon: 'production', to: '/dashboard' },
  { id: 'hmi', title: 'HMI', description: 'Line technician access to run, update and monitor lines.', tone: 'mint', icon: 'hmi', to: '/hmi' },
  { id: 'engineering', title: 'Engineering Workspace', description: 'Build, configure and improve.', tone: 'amber', icon: 'engineering', to: '/engineering' },
  { id: 'qa', title: 'QA', description: 'Review quality checks and production concerns.', tone: 'red', icon: 'qa', to: '/dashboard/qa' },
  { id: 'intelligence', title: 'Operational Intelligence', description: 'Turn data into action with powerful insights.', tone: 'purple', icon: 'intelligence', to: '/dashboard/operational-intelligence' },
  { id: 'cleaning', title: 'Cleaning Plant', description: 'Track rice, storage, movement and Weight Through Time.', tone: 'cyan', icon: 'performance', notice: 'Planned: rice processing, material movements, silo levels, throughput and Weight Through Time. This area is not available yet.' },
  { id: 'management', title: 'Management', description: 'People, planning and factory oversight.', tone: 'ice', icon: 'management', to: '/management' },
  { id: 'reports', title: 'Reports', description: 'Generate and view operational reports.', tone: 'slate', icon: 'reports', to: '/management/reports' },
  { id: 'settings', title: 'Settings', description: 'Configure your system and preferences.', tone: 'slate', icon: 'settings', notice: 'Under development. Existing production standards, weekly targets and LineTech setup remain in Management.' },
  { id: 'performance', title: 'Performance', description: 'Review technician and production performance.', tone: 'green', icon: 'performance', to: '/management/performance' },
]
export const QUICK_ACCESS = ['hmi', 'warehouse', 'engineering', 'intelligence'].map(
  (id) => DESTINATIONS.find((item) => item.id === id)!,
)
