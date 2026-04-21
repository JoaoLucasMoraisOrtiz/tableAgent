# MCP Tools — Agent Usage Guide

You have access to two MCP servers: **browser** (visual automation) and **dataflow** (code analysis). Use them together to understand complete systems: what the user sees on screen + what happens in the code.

---

## 1. Browser MCP

Browser automation via Playwright. Lets you see and interact with web applications.

### Mandatory setup
Always call `set_project_dir` at the start of the session with the current working directory. Without this, `screenshot` won't work.

### Tools

| Tool | When to use |
|---|---|
| `set_project_dir(path)` | **First thing.** Sets where screenshots are saved. |
| `navigate(url)` | Open a URL. Waits for `networkidle` by default. |
| `screenshot()` | Capture the screen. Returns the file path. **Always read the file with `fs_read` mode `Image` afterwards.** |
| `click(x, y)` | Click at coordinates. Use the previous screenshot to determine x,y. |
| `type_text(text)` | Type into the focused element. |
| `press_key(key)` | Special keys: Enter, Tab, Escape, ArrowDown, etc. |
| `get_page_info()` | Current URL, title, viewport. Lightweight — use before screenshot when you only need the URL. |
| `list_tabs()` | List open tabs. |
| `switch_tab(index)` | Switch to a tab. |
| `toggle_devtools()` | Open/close F12. |

### Typical flow
```
1. set_project_dir("/path/to/project")
2. navigate("http://localhost:3000")
3. [user navigates manually]
4. screenshot() → fs_read(Image) → describe the screen
5. click(x, y) → screenshot() → fs_read(Image) → confirm result
```

### Rules
- **Never** read the screenshot as base64 in context. Always use `fs_read` mode `Image`.
- After each `click`, take a `screenshot` to confirm the result.
- If the user needs to log in manually, use `navigate` to open the browser and wait for them to confirm.

---

## 2. Dataflow MCP

Multi-language static code analysis. Builds a dependency graph using tree-sitter.

### Supported languages
JavaScript, TypeScript, Java, Python, PHP.

### Auto-detected frameworks
Next.js, Nuxt.js, Angular, Svelte, Vite, React, Vue, Express, Fastify, Spring Boot, Django, FastAPI, Flask, Laravel, Rails.

### Tools

| Tool | When to use |
|---|---|
| `index_project(path)` | **First thing.** Scans the project, detects frameworks, parses files, builds the graph. |
| `set_framework(frameworks)` | Correct wrong detection. Comma-separated string: `"nextjs,spring-boot"`. Only use if the user requests a correction. |
| `get_connections(file, symbol?)` | View a file's connections. Without `symbol`: shows signatures. With `symbol`: expands details (calls, lines). |
| `trace_flow(entry, max_depth?)` | Trace data flow. Format: `"file.tsx"` or `"file.tsx::functionName"`. |

### Typical flow
```
1. index_project("/path/to/project")
   → Returns detected frameworks + statistics
2. Confirm frameworks with the user. Correct with set_framework if needed.
3. get_connections("page.tsx")
   → View imports, dependents, symbol signatures
4. get_connections("page.tsx", "MyFunction")
   → Expand: see calls, decorators, exact lines
5. trace_flow("page.tsx::MyFunction")
   → Follow the flow: which functions are called, in which files
```

### Hierarchical graph
The graph is **hierarchical by design** to save context:
- `get_connections(file)` → signatures only (lightweight)
- `get_connections(file, symbol)` → details of a specific symbol
- If you need the source code, use `fs_read` with the returned lines (`line_start`-`line_end`)

### Rules
- **Always** start with `index_project`. Without it, no other tool works.
- Respect the hierarchy: start with the overview, expand on demand.
- `trace_flow` defaults to `max_depth=6`. Increase if the flow goes deeper.
- Files ignored by `.gitignore` and common patterns (node_modules, target, dist, etc.) are automatically excluded.

---

## Combined usage: Browser + Dataflow

When the user wants to understand what happens when interacting with the application:

```
1. Browser: screenshot → identify the current page/route
2. Dataflow: get_connections on the page/route file
3. Dataflow: trace_flow to follow the data flow
4. Dataflow: get_connections with symbol to expand specific functions
5. fs_read with specific lines if you need to see the source code
6. Browser: click to navigate → repeat
```

This connects the **visual** (what the user sees) with the **code** (what happens underneath).
