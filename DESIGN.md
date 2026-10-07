# Dashboard design

The user's brief fixes a minimal, light operational interface. Use a neutral
paper background, white working surfaces, dark ink and one blue action color.
System sans-serif for controls; monospace only for endpoints and measurements.

The proxy table is the primary workspace. Summary counts remain compact and
comparable. Import and strategy controls form a separate narrow desktop column;
mobile stacks them below the table, with a visible jump link to import.

Status is always expressed in words, not only color. Visible focus rings,
labelled inputs, useful empty/error/loading states, and reduced-motion support
are required. Table overflow stays inside its labelled scroll region.

All data/HTML is rendered by Jinja2. The browser connects WebSocket notifications
to HTMX out-of-band updates; reconnect and periodic snapshot refresh recover
missed events. No client-side model or template system.
