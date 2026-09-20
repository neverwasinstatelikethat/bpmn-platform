# Frontend design system

## Intent

The BPMN platform is a calm, practical workspace. Interfaces should make a
process legible before they try to look expressive: one clear primary action,
quiet surfaces, thin dividers, and only semantic status colour.

## Typography

- **Euclid Circular A** (`--font-sans`) is used for body copy, controls, data,
  tables, and navigation.
- **Villula** (`--font-display`) is used for page and campaign headings. It may
  use deliberate mixed case for short display phrases, but never for dense UI
  labels, tables, or form controls.

## Tokens and surfaces

`src/styles/tokens.css` is the only source for colour, spacing, radius, shadow,
and motion tokens. New UI uses semantic variables rather than raw hex values.

- Canvas: warm cream; product surfaces: white and muted pastel states.
- Green is the action colour. Berry, amber, and green status colours describe
  errors, caution, and success respectively; text or icons accompany colour.
- Working views use constrained content widths, one elevation level, and thin
  hairline borders. Avoid glass blur, decorative gradients, and stacked cards.

## Motion and controls

- Aurora is limited to the landing hero behind content.
- The landing gallery uses image trail only for fine pointers and an accessible
  slider fallback elsewhere.
- AI answer text reveals at 50 ms per character; all timers are cancelled on
  unmount. Reduced-motion users see stable final text.
- Luminous switches are only for reversible local view preferences, never
  server mutations or destructive choices.

## Route inventory

Public: `/`, `/login`, `/register`, `/reset-password`, `/share/:token` (read-only
BPMN preview for external recipients; `can_edit` shares offer "open a copy" in
the editor), and a 404 state.
Protected: `/editor`, `/guideline`, `/errors`, `/my-schemas`, `/profile`, and
`/invite/:token`. A protected redirect keeps the original route so an invited
user returns to the acceptance flow after sign-in.

## Known gaps

- The legacy BPMN canvas integration still uses bpmn-js and PrimeReact widgets
  internally. New shell, chat, and analysis styling is token-based; a future
  isolated editor-controller extraction can remove those remaining dependency
  details without changing process behaviour.
- Personal and team folders are fully wired: the registry scopes folders by
  `team_id`, team members can move shared diagrams into team folders, and
  diagram rows offer a "В команду…" share action backed by
  `/api/diagrams/share-to-team`. Profile creates teams, lists members, and
  invites by role; the invite response carries `accept_link` for hand-off.
