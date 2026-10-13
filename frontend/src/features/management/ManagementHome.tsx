import { Link } from 'react-router-dom'

type CardState = 'open' | 'phase2'

interface ManagementCard {
  title: string
  description: string
  state: CardState
  /** Only working areas link anywhere - never a dead link. */
  to?: string
}

/** Descriptions of each area - no figures, counts or statuses here. */
const CARDS: ManagementCard[] = [
  { title: 'Shift reports', description: 'Generate, retrieve and export shift and 24-hour reports.', state: 'open', to: '/management/reports' },
  {title: 'Production standards', description: 'Versioned speed standards for new runs, with management approval history.', state: 'open', to: '/management/production-standards'},
  {
    title: 'Production dashboard',
    description: 'Expected against actual output, downtime and which line needs attention.',
    state: 'open',
    to: '/dashboard',
  },
  {
    title: 'Technician performance',
    description: 'Completed runs against target for each line technician.',
    state: 'open',
    to: '/management/performance',
  },
  {
    title: 'Factory setup',
    description: 'LineTech machine groups, fault categories, planned stops and changeover choices.',
    state: 'open',
    to: '/management/linetech',
  },
  {
    title: 'Active runs',
    description: 'Every run in progress, with a controlled force-close and reason.',
    state: 'open',
    to: '/management/active-runs',
  },
  {
    title: 'Weekly targets',
    description: 'Factory weekly tonnage, effective week, reporting start day and target notes.',
    state: 'open',
    to: '/management/weekly-targets',
  },
]

const BADGE: Record<CardState, string> = { open: 'Open', phase2: 'Phase 2' }

export function ManagementHome() {
  return (
    <section className="management-home" aria-labelledby="management-home-heading">
      <h1 id="management-home-heading">Management</h1>
      <p className="management-muted">
        Choose an area. Areas marked <strong>Phase 2</strong> are planned for a later release.
      </p>

      <ul className="management-cards">
        {CARDS.map((card) => {
          const body = (
            <>
              <span className={`management-card__badge management-card__badge--${card.state}`}>
                {BADGE[card.state]}
              </span>
              <h2 className="management-card__title">{card.title}</h2>
              <p className="management-card__description">{card.description}</p>
              {card.to && (
                <span className="management-card__action" aria-hidden="true">
                  Open →
                </span>
              )}
            </>
          )
          return (
            <li key={card.title}>
              {card.to ? (
                <Link to={card.to} className="management-card management-card--link">
                  {body}
                </Link>
              ) : (
                <div className="management-card management-card--phase2" aria-disabled="true">
                  {body}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
