# Home navigation hub - review build

Implemented on `feature/linetech-preview`. This document retains the original UI review evidence; check Dokploy for the deployed commit.

## Connected destinations

| Card | Existing route | Access |
| --- | --- | --- |
| Home | `/` | Public; retains a validated Management session |
| HMI | `/hmi` | Existing tablet sign-in/device gate |
| Engineering Workspace | `/engineering` | Existing Engineering sign-in |
| Management | `/management` | Existing Management sign-in |
| Reports | `/management/reports` | Existing Management session |
| Performance | `/management/performance` | Existing Management session |
| Production | `/dashboard` | Existing Management session |
| QA | `/dashboard/qa` | Existing Management session |
| Operational Intelligence | `/dashboard/operational-intelligence` | Existing Management session |

Quick Access and All Destinations share `features/home/navigation.ts`. Cards are single React Router links or buttons, not nested controls. Warehouse (Phase 2), Cleaning Plant (planned) and Settings (under development) have no existing routes and open accessible information dialogs. Notifications also explains its current availability; Search filters actual destinations and Help explains where to work.

## Authentication and workflows

Home is inside the existing ManagementSessionRoot but outside RequireManagementSession. The server still validates restored sessions; Home grants no permissions. Moving Home -> Management/dashboard -> Home keeps the same provider and session. Moving to HMI or Engineering still unmounts the provider and revokes the Management token, preserving shared-tablet safety. Engineering retains its existing memory-only session lifecycle and separate sign-in. Logout and expiry remain unchanged. Public Home shows no mock user, notification count or production figures.

Root now opens Home rather than redirecting to HMI. Existing deep links remain valid. Home controls are available in the HMI/Management shell, dashboard sidebar/topbar and Engineering page. No backend, database, calculations, production workflows or operational data were changed.

## Design and preview

Built from the supplied screenshot because no Home source component existed. Retains navy background, coloured destination cards, sidebar, Quick Access and the exact tagline. Uses the existing TonnageFlow brand asset. Factory background is a new lightweight SVG illustration, not the unavailable source photograph; icons are local SVGs. Performance is an additional full-width destination. These adaptations are visible for review, not claimed as a pixel-identical copy.

Screenshots: `home-preview/desktop.png`, `tablet-landscape.png`, `tablet-portrait.png`, `mobile.png`. API unavailable is intentional in screenshots: the local browser test blocks backend calls with 503 fixtures, so it cannot read or modify production data.

## Validation

- Full frontend suite: 578 tests passed across 46 files, including 13 new Home tests.
- TypeScript and production build passed. Vite retains its >500 kB bundle advisory.
- ESLint passed.
- Real Chromium/Edge checks at 1672x941, 1180x820, 820x1180 and 390x844: no horizontal overflow or JavaScript errors.
- Tested all four future-feature dialogs, keyboard focus containment, Escape dismissal and focus return, search, browser back, protected destination login, HMI/Engineering destinations and return Home.
- Unit integration verifies validated session retention through Home/Production/back and revocation on entering HMI. Existing authentication regression tests pass.
- Browser fixture verifies unauthenticated navigation; no live credentials or production writes used.

Reproduce visual checks with a local Vite server at `127.0.0.1:5181`, then run `python scripts/manual_integration/home_screenshots.py` using the existing Playwright environment.

## Files

New:
- `frontend/src/features/home/HomePage.tsx`
- `frontend/src/features/home/HomeIcon.tsx`
- `frontend/src/features/home/home.css`
- `frontend/src/features/home/navigation.ts`
- `frontend/src/features/home/HomePage.test.tsx`
- `frontend/public/home-factory.svg`
- `scripts/manual_integration/home_screenshots.py`
- This report and `docs/home-preview/` screenshots/verification JSON.

Updated:
- `frontend/src/routes/AppRoutes.tsx` and `AppRoutes.test.tsx`
- `frontend/src/layouts/AppShell.tsx`
- `frontend/src/pages/EngineeringPage.tsx`
- `frontend/src/features/dashboard/shell/DashboardLayout.tsx` and `navigation.ts`
- `frontend/src/features/management/session/ManagementSessionProvider.tsx` (documentation only).
