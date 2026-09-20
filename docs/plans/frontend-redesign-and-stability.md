# Implementation plan: unified frontend redesign and stability

## Goal

Migrate every current React route to the approved VkusVill × Softly system while preserving the existing FastAPI contracts. The result must use one token contract, shared layout and feedback primitives, dependable API access, accessible reduced-motion behaviour, and a consistent visual density for working screens.

## Scope and decisions

- Routes in scope: `/`, `/login`, `/register`, `/editor`, `/guideline`, `/errors`, `/my-schemas`, `/profile`, plus the existing but currently unregistered invitation-acceptance flow.
- Do **not** add Tailwind to this CRA application. Its value from the requested Tailwind patterns is applied as a constrained utility vocabulary in CSS tokens and component classes; a second styling runtime would duplicate the existing token system and make the migration less stable.
- Reconcile `design.md` and `src/styles/tokens.css` before page work. Use Euclid Circular A for body, forms, data, and navigation; use Villula for page and campaign headings only. Short intentional mixed-case Villula display treatment is allowed, but dense UI labels remain Euclid.
- Keep the product light. Aurora is a low-contrast, clipped decorative layer only on the landing hero; it must not use a dark background, gradients in text, or affect controls/readability. Luminous switches are reserved for live, reversible product preferences and simulated preview state, not destructive actions.
- Use the real asset inventory. The current landing references six missing `/static/*-screenshot.png` files; first derive a small approved gallery from the available `static/bpmn_diagram.png` and SVG assets, or capture sanitized local UI screenshots during the work. Do not show broken image URLs.
- Keep all server requests behind a shared Axios client that uses `API_BASE_URL`, injects the current bearer token, normalizes safe user-facing errors, and never stores server registry data as durable browser state.

## Target frontend structure

```text
src/
  app/                AppProviders, AppRoutes, route-level error boundary
  api/                client.js, registry.js, account.js, process.js
  components/
    layout/            AppShell, PageHeader, WorkPage, EmptyState, PageLoader
    ui/                token-backed controls and motion primitives
  features/
    auth/              LoginPage, RegisterPage, InviteAcceptPage, auth forms
    editor/            EditorPage and existing editor subpanels
    registry/          RegistryPage, FolderTree, FolderCard, DiagramTable
    learning/          GuidelinePage, ErrorsPage
    profile/           ProfilePage, team and role panels
  styles/              tokens.css, page-layout.css
```

The migration moves only page-local view code into features. BPMN editing, authentication state, and FastAPI endpoints retain their public behaviour. `AuthContext` remains the session owner; it delegates HTTP configuration to `api/client.js` rather than setting global Axios state itself.

## Parallel, limited legacy refactoring

This is a supporting workstream, not a second project. Apply it only while touching the named files in Tasks 2–9; it must not delay a user-visible route migration or expand into a backend rewrite.

1. Treat the following as **deep modules** with deliberately small **interfaces**:
   - `api/client.js`: `request`, `toUserMessage`, session-token configuration and request cancellation; page callers must not know Axios interceptor details.
   - `features/registry`: `loadRegistry`, `createFolder`, `moveDiagram`, `restoreDiagram`, plus a view model for the tree; pages must not know endpoint strings or folder-tree traversal details.
   - `features/editor`: a controller owning modeler lifecycle and process actions; visual panels receive explicit state and callbacks, never a bpmn-js instance.
   - `components/ui`: token-backed visual controls with documented props and accessibility behaviour; pages must not duplicate focus, disabled, or reduced-motion CSS.
2. Put a **seam** at each of those interfaces. Tests cross the same seam as callers: mock the HTTP adapter for registry/auth tests, and inject only a modeler adapter in editor-controller tests. Do not introduce a seam where there is only one concrete behaviour and no test need.
3. Improve **locality** by moving duplicated request construction, toast/error mapping, timing cleanup, and token references from route files into the relevant module. Do not create one-method pass-through modules: a module earns its **depth** only when it hides meaningful policy shared by several callers.
4. Adopt a single token interface for all changed CSS. Use semantic token variables, never legacy aliases or raw hex values except when defining `tokens.css`. Migrate each page and delete an alias only when `rg` shows no remaining caller. This gives the design system one source of truth without a risky all-at-once rewrite.
5. For each extracted module, document its public interface in a short file header or adjacent README: allowed inputs, result shape, error mode, cancellation/cleanup responsibility, and whether it owns persisted state. The implementation remains private to callers.

## Work sequence

### 1. Establish a safe baseline and an endpoint inventory

Files:

- `bpmn-constructor/package.json`
- `bpmn-constructor/src/App.js`
- `bpmn-constructor/src/context/AuthContext.js`
- `main.py`

1. Run `npm run build` before modifications and record any existing warnings.
2. Build a route-to-API matrix from `App.js`, `AuthContext.js`, `Editor.js`, `MySchemas.js`, and `Profile.js`. Verify every request in the matrix exists in `main.py`, including `/api/achievements`, `/api/activity`, team-folder routes, and invitation acceptance.
3. Mark a request as unavailable only after this trace. For unavailable optional widgets, render a clear empty state rather than issuing a repeated failing request.
4. Add a minimal test harness based on CRA's existing Testing Library dependencies and a `renderWithProviders` helper wrapping `MemoryRouter`, `AuthProvider`, and reduced motion.

Example smoke test to add:

```js
it('keeps public routes reachable', () => {
  renderWithProviders(<App />, { route: '/login' });
  expect(screen.getByRole('heading')).toBeInTheDocument();
});
```

### 2. Make the visual system canonical and add shared work-screen primitives

Files:

- `bpmn-constructor/design.md`
- `bpmn-constructor/src/styles/tokens.css`
- `bpmn-constructor/src/index.css`
- `bpmn-constructor/src/components/ui/ui.css`
- new `bpmn-constructor/src/components/layout/{AppShell,WorkPage,PageHeader,EmptyState,PageLoader}.jsx`
- new `bpmn-constructor/src/components/layout/layout.css`

1. Remove legacy aliases and the unused gradient token only after all consuming pages have moved to semantic tokens. New code may use only `--color-*`, `--space-*`, `--radius-*`, `--shadow-*`, and `--duration-*` variables.
2. Extend tokens with a compact working-screen scale: page gutter, content max-width, sidebar width, table row height, focus ring, disabled opacity, and semantic status foreground/background pairs that meet WCAG AA contrast.
3. Set Euclid Circular A as `--font-sans` and Villula as `--font-display`. Use the display font only for headings and short display treatment; update base headings to 600 weight and `--tracking-tight`; do not use decorative all-caps/negative tracking outside the existing token contract.
4. Implement `AppShell` with a stable nav offset, `WorkPage` with `main` landmark and title/description/actions slots, `PageHeader` for title/action hierarchy, and shared loading/error/empty states. Use borders and restrained shadows to establish elevation; do not introduce nested cards.
5. Keep focus visibility, responsive spacing, and `prefers-reduced-motion` in the shared layer so pages do not reimplement them.

Acceptance checks:

- Colour is semantic, not the only indicator of score, errors, or role.
- Form fields and tables have visible focus and at least 44px pointer targets.
- Desktop working screens have a constrained content width; narrow screens collapse nonessential side panels below content.

### 3. Centralize HTTP behaviour and route fault containment

Files:

- new `bpmn-constructor/src/api/client.js`
- new `bpmn-constructor/src/api/{account,registry,process}.js`
- `bpmn-constructor/src/context/AuthContext.js`
- new `bpmn-constructor/src/app/{AppProviders,AppRoutes,RouteErrorBoundary}.jsx`
- `bpmn-constructor/src/App.js`

1. Create one Axios instance with `baseURL: API_BASE_URL`, a request interceptor reading the in-memory/local session token, a response interceptor that clears an expired session once, and `toUserMessage(error)` that returns a generic Russian error for network/5xx cases without surfacing server internals.
2. Move endpoint calls into named methods such as `registryApi.getTree()`, `registryApi.moveDiagram({ diagramId, folderId })`, `accountApi.updateProfile(data)`, and `processApi.improve(xml)`. Preserve endpoint method, payload, and authorization header exactly as verified in Task 1.
3. Refactor `AuthContext` to expose session state/actions only; it must not mutate `axios.defaults` or make a speculative diagrams request on every login. The client owns request configuration.
4. Split top-level app composition into providers and routes. Register `/invite/:token` (matching the parameter consumed by `InviteAccept.js`) as a protected route and add an explicit catch-all Not Found state.
5. Add a route error boundary with a retry button and contextual route title. It must log only non-sensitive diagnostic metadata to the console in development.

Example contract test:

```js
it('maps a backend failure to a safe Russian message', () => {
  expect(toUserMessage({ response: { status: 500 } }))
    .toBe('Сервис временно недоступен. Попробуйте ещё раз.');
});
```

### 4. Finish and harden the landing experience

Files:

- `bpmn-constructor/src/Home.js`
- `bpmn-constructor/src/Home.css`
- `bpmn-constructor/src/components/ui/{Aurora,ImageTrail,Slider,Switch,Typewriter}.jsx`
- `bpmn-constructor/src/components/ui/ui.css`
- `bpmn-constructor/src/config.js`

1. Replace hard-coded, nonexistent screenshot paths with an `assetUrl(name)` helper based on `API_BASE_URL`, a verified manifest, and a nonempty image fallback. Build the image trail/gallery only from URLs that respond successfully in local verification.
2. Keep `Aurora` clipped within `.hero`, behind content, with pastel green/lavender/peach shapes at low opacity and no interactive layer. Freeze motion under reduced motion.
3. Ensure the main AI answer headline uses `Typewriter` at `speed={50}` and is announced once to assistive technology after completion. Prevent the looping preview typewriter from repeatedly announcing its changing text.
4. Use `ImageTrail` on fine pointers only; preserve its current slider fallback for touch/reduced-motion. Cap active image nodes and clear timers on unmount. The slider must support keyboard arrows, touch, pause on hover/focus, explicit dot labels, and no autoplay when reduced motion is requested.
5. Retain the preview switch as a reversible mock/live-preview control. Use it to toggle chart emphasis and explanatory text; do not allow it to impersonate a persisted server setting.
6. Remove `QuickStartPanel`, `FeatureCard`, and `SchemaCarousel` and their CSS only after `rg` proves they have no imports. Do not delete shared assets used elsewhere.

Landing tests:

```js
it('uses a 50ms main typewriter delay', () => {
  render(<Typewriter text="ИИ готов" speed={50} />);
  jest.advanceTimersByTime(50);
  expect(screen.getByText(/И/)).toBeInTheDocument();
});

it('renders a keyboard-accessible fallback gallery', () => {
  mockFinePointer(false);
  render(<ImageTrail images={[{ src: '/static/bpmn_diagram.png', alt: 'Схема' }]} />);
  expect(screen.getByRole('region', { name: 'Скриншоты продукта' })).toBeInTheDocument();
});
```

### 5. Redesign registration, sign-in, reset and invitation acceptance

Files:

- `bpmn-constructor/src/{Login,Register,InviteAccept}.{js,css}`
- `bpmn-constructor/src/BPMNAnimation.{js,css}`
- new `bpmn-constructor/src/features/auth/{AuthLayout,AuthForm,PasswordField}.jsx`

1. Replace randomly generated particles, progress bars, duplicated Toast wiring, and competing two-panel animations with `AuthLayout`: calm informative panel on wide viewports and a single focused form column on mobile.
2. Use shared Input/Button/Alert primitives. Keep client validation for empty fields and password confirmation, but render API failure with `toUserMessage`; never reveal whether an account exists in password recovery.
3. Keep the BPMN animation as an optional, static/reduced-motion-safe illustration. If it cannot be rendered without layout shifts, replace it with a CSS/SVG process sketch using product tokens.
4. Preserve `?email=` prefill from landing registration and `?token=` reset flow. Clear form errors on input correction and disable submit exactly while the request is active.
5. Give invitation acceptance explicit states: login required (with return path), processing, accepted, expired/used/error, and navigate only after visible feedback. Token strings must never be displayed or logged.

### 6. Rebuild the registry around interactive, real folder data

Files:

- `bpmn-constructor/src/MySchemas.{js,css}`
- new `bpmn-constructor/src/features/registry/{RegistryPage,RegistryToolbar,FolderTree,FolderCard,DiagramTable,FolderDialog}.jsx`
- new `bpmn-constructor/src/features/registry/registry.css`
- `bpmn-constructor/src/api/registry.js`
- `main.py` only if Task 1 verifies a missing contract that the user flow requires

1. Preserve the server-backed personal folder tree and diagram records. Replace the current connector-line tree with accessible `tree`/`treeitem` semantics, keyboard expand/collapse, selected-folder filtering, and animated height transitions that respect reduced motion.
2. Adapt the supplied interactive-folder reference into `FolderCard`: a button opens a folder visually, exposes up to three diagram sheets, and shows metadata/action controls in the registry context. Use token radii/shadows and green action accent; no external Tailwind classes, purple branding, glass blur, or decorative gradients.
3. Keep create, delete, move, import, restore, and open-diagram actions bound to their verified API methods. Replace `window.confirm` with a controlled confirmation dialog that names the folder/diagram and describes whether contents are preserved.
4. Eliminate guessed team behaviour (`teams[0]`, empty `teamFolderTree`, and lookup through un-fetched `team.diagrams`). Add a team selector only when a verified team-folder endpoint exists; otherwise present team diagrams as a read-only registry with an explanatory state and do not render unavailable folder actions.
5. Use table/list responsive variants, debounced local search, score badge text plus icon/label, and loading/error/empty states. Preserve owner/role restrictions supplied by the backend; frontend hiding is not authorization.

Registry API test cases:

```js
it('moves a diagram with the documented payload', async () => {
  await registryApi.moveDiagram({ diagramId: 'diagram-1', folderId: 'folder-2' });
  expect(mock.post).toHaveBeenCalledWith('/api/diagrams/move-to-folder', {
    diagram_id: 'diagram-1', folder_id: 'folder-2'
  });
});

it('does not render team-folder controls without a verified endpoint', () => {
  render(<RegistryPage teamFolderSupport={false} />);
  expect(screen.queryByRole('button', { name: /создать папку/i })).not.toBeInTheDocument();
});
```

### 7. Migrate the editor without disrupting BPMN interactions

Files:

- `bpmn-constructor/src/Editor.{js,css}`
- `bpmn-constructor/src/{GenerateChat,ImproveChat,AiChat,ScorePanel,ThinkBlock,TypewriterMessage}.{js,css,jsx}`
- new `bpmn-constructor/src/features/editor/{EditorShell,EditorToolbar,AssistantPanel,AnalysisPanel}.jsx`
- `bpmn-constructor/src/api/process.js`

1. Split the editor shell from the bpmn-js integration so modeler lifecycle, XML import/export, auto-layout, and server actions remain in the existing editor controller. Do not remount the modeler when a side panel or display preference changes.
2. Introduce a dense-but-calm `EditorShell`: compact top toolbar, central canvas, collapsible assistant/analysis panels, mobile drawer, and clear save status. Use text labels/tooltips alongside icons.
3. Apply 50ms character reveal to newly completed AI assistant answers only. Stream/animate the visible message from left to right, cancel timers on navigation/new request, show a non-animated final string under reduced motion, and never type a backend error as if it were an answer.
4. Place one luminous switch in the analysis panel for AI hints, backed by local view state. It must not trigger generation/evaluation or alter server data. Keep save/delete/share controls conventional and clearly labelled.
5. Preserve existing generate → validated BPMN → render/auto-layout; evaluate; improve → pending improvement → accept/reject; save; and share workflows. Surface request failures inline with retry while retaining unsaved XML.

### 8. Migrate learning and profile/team routes

Files:

- `bpmn-constructor/src/{Guideline,Errors,Profile}.{js,css}`
- new `bpmn-constructor/src/features/learning/{LearningLayout,GuidelineList,ErrorPatternCard}.jsx`
- new `bpmn-constructor/src/features/profile/{ProfilePage,TeamPanel,RolePanel}.jsx`

1. Convert Guideline and Errors to shared `WorkPage`, readable long-form grids, ordered examples, and accessible accordions. Keep representative BPMN source data separate from rendering components and avoid turning internal mechanics into navigation.
2. Convert Profile to tabs/sections with a personal-data form, teams, roles, and activity. Each data pane independently loads, errors, and retries so an unavailable optional endpoint cannot block profile editing.
3. Use switches only for a real, available profile preference; if no server contract exists, do not add a fake persisted setting. Use buttons/dialogs for role, invitation, and team actions, always showing the current permission constraint.
4. For any profile endpoint missing from `main.py`, either add a protected backend implementation with object-level checks and PostgreSQL-safe SQLAlchemy operations, or replace the feature with an honest unavailable/empty state after product confirmation. Do not fabricate endpoint responses in the frontend.

### 9. Update navigation, loading boundaries, and document the completed system

Files:

- `bpmn-constructor/src/{App,Header,ProtectedRoute}.{js,css}`
- `bpmn-constructor/src/app/AppRoutes.jsx`
- `bpmn-constructor/design.md`
- `bpmn-constructor/README.md`

1. Make the nav route-aware: public pages show sign-in/register; authenticated pages expose Editor, Registry, Learning, and Profile with active state. Do not show routes absent from `AppRoutes`.
2. Add lazy route loading for the editor and profile/registry routes, a suspense fallback that preserves shell layout, and a protected-route loading state that does not flash protected content before `/api/me` resolves.
3. Add a catch-all route, request cancellation on page unmount where appropriate, and no-op protection against state updates after unmount.
4. Update `design.md` only after all visuals are accepted: remove Known Gaps that are completed, document UI architecture, route inventory, actual asset policy, approved switch usage, and animation/reduced-motion constraints. Update README with product capabilities rather than component filenames.

### 10. Verify observable behaviour and remove migration debris

Files:

- all files above, plus test files in `bpmn-constructor/src/**/*.test.js`

1. Run unit/component tests for `api/client`, typewriter, slider, folder tree, route error boundary, and auth return-path handling.
2. Run `npm run build` with no compile errors. Resolve ESLint/react-hook warnings introduced by the migration.
3. Start backend and frontend. Manually verify, at desktop and narrow viewport:
   - landing Aurora stays background-only; typewriter is 50ms; image trail works on mouse and gallery fallback works on touch;
   - register/login/reset, protected redirect, and invitation states;
   - create folder, expand/open interactive folder, move diagram, import, delete/restore, and role-limited team view;
   - editor opens a diagram, generates or handles provider failure, renders, auto-layouts, evaluates, improves, accepts/rejects, saves, shares;
   - guideline, typical errors, profile, team/role states, and navigation/catch-all.
4. Use browser devtools/network log to confirm all requests use the configured API base and no page emits requests to unsupported endpoints.
5. Audit keyboard navigation, focus state, text contrast, and `prefers-reduced-motion` on every route. Check no token, password, invitation token, XML content, or server stack trace appears in UI/logging.
6. Delete only files proved unused by `rg` and update imports. Do not alter `.gitignore`.

## Sequencing and checkpoints

1. Complete Tasks 1–3 before page migrations; they are the stability foundation.
2. Complete Task 4 next, then obtain a visual check of the landing before treating its design as final.
3. Tasks 5–8 can be executed in parallel after Tasks 1–3, but integration in Task 9 happens once all are merged.
4. Task 10 is the release gate. Do not claim completion if the backend is unavailable for the required registry/editor/auth journey; state which live checks could not be run.

## Plan self-review

- Coverage: every requested enhancement maps to Tasks 4 (Aurora, slider, image trail, 50ms typewriter, switches), 6 (interactive folders), and 2–10 (all routes, architecture, stability).
- Stability: Tasks 1, 3, 6, 7, 9, and 10 trace API contracts, keep server authority intact, and prevent broken assets/unsupported requests.
- Design: Task 2 makes tokens canonical; Tasks 4–8 apply hierarchy, constrained spacing, restrained depth, responsive layouts, and motion guardrails derived from the requested design skills.
- No Tailwind runtime migration: explicitly excluded as it would add a conflicting CSS architecture to the existing CRA/token stack.
