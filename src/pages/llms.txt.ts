import type { APIContext } from 'astro';
import { getCollection } from 'astro:content';
import { readdirSync, readFileSync, type Dirent } from 'node:fs';
import { join } from 'node:path';
import {
  LLMS_LIMITS,
  cleanSiteName,
  collectionSection,
  humanize,
  isPlaceholderDescription,
  isPlaceholderName,
  jsonLdDescription,
  oneLine,
  pageMeta,
  renderLlmsTxt,
  stripSiteAffix,
  type LlmsCollection,
  type LlmsEntry,
  type LlmsLink,
  type LlmsSection,
} from '../lib/llms';

// AUTO-GENERATED (Willo). Serves /llms.txt, rebuilt from the site's own data
// on every deploy: identity from site-config, pages from src/pages, entries
// from the content collections. Do not add a public/llms.txt — a file there
// and this route both claim /llms.txt.
export const prerender = true;

// Optional modules are globbed lazily, never imported: a repo without one of
// them must still build, and a module that throws on load does so inside the
// fallback below. The legacy src/content/config.* is never globbed — Rollup
// bundles every glob target, so a stale legacy file importing a removed
// package would fail the build even unused; its names are read as text.
const siteConfigs = import.meta.glob<{ default?: any }>('../site-config.{ts,js,mjs}');
const contentConfigs = import.meta.glob<{ collections?: Record<string, unknown> }>(
  '../content.config.{ts,mts,js,mjs}',
);

async function firstModule<T>(
  loaders: Record<string, () => Promise<T>>,
  prefixes: string[],
): Promise<T | undefined> {
  for (const prefix of prefixes) {
    const key = Object.keys(loaders).sort().find((k) => k.startsWith(prefix));
    if (key) return loaders[key]();
  }
  return undefined;
}

async function loadSiteConfig(): Promise<any> {
  try {
    return (await firstModule(siteConfigs, ['../site-config.']))?.default ?? {};
  } catch {
    return {};
  }
}

// Astro reads src/content.config.* and falls back to the legacy location.
async function loadCollectionNames(root: string): Promise<string[]> {
  try {
    if (Object.keys(contentConfigs).length) {
      const mod = await firstModule(contentConfigs, ['../content.config.']);
      return Object.keys(mod?.collections ?? {});
    }
  } catch {
    return [];
  }
  for (const ext of ['ts', 'mts', 'js', 'mjs']) {
    const source = readText(join(root, 'src', 'content', `config.${ext}`)).slice(0, 200_000);
    const decl = /(?:export\s+)?const\s+collections\s*(?::[^=\n]{0,200})?=\s*\{([^}]*)\}/.exec(source);
    if (!decl) continue;
    return decl[1]
      .split(',')
      .map((part) => part.split(':')[0].trim().replace(/^['"`]|['"`]$/g, ''))
      .filter((name) => /^[\w-]+$/.test(name));
  }
  return [];
}

// Routes no crawler should be pointed at — the same families astro.config.mjs
// keeps out of the sitemap, plus error pages.
const EXCLUDED = /^\/(?:app|login|signup|cart|checkout\/success|checkout\/cancel|404|500)(?:\/|$)/;
// The `pages` collection holds standalone prose pages (terms, privacy). They
// are listed as pages, not as a collection.
const PAGE_COLLECTIONS = new Set(['pages']);
const PAGE_EXT = /\.(astro|md|mdx)$/;

function readText(path: string): string {
  try {
    return readFileSync(path, 'utf8');
  } catch {
    return '';
  }
}

function readJson(path: string): any {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return undefined;
  }
}

interface PageFile {
  path: string; // URL path with trailing slash
  file: string;
  ext: string;
}

// URL folders ('/', '/attorneys/') holding a dynamic route: `rest` for a
// `[...x]` catch-all (serves any depth below), `one` for `[slug]` (one level).
interface DynamicRoutes {
  rest: string[];
  one: string[];
}

function walkPages(dir: string, dynamic: DynamicRoutes, rel = ''): PageFile[] {
  let entries: Dirent[];
  try {
    entries = readdirSync(join(dir, rel), { withFileTypes: true });
  } catch {
    return [];
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  const out: PageFile[] = [];
  for (const entry of entries) {
    const name = entry.name;
    // Symlinks are not followed: a loop would hang the build, and a link out
    // of the repo would publish paths from elsewhere on the build host.
    if (entry.isSymbolicLink() || name.startsWith('_') || name.startsWith('.')) continue;
    const child = rel ? `${rel}/${name}` : name;
    if (entry.isDirectory()) {
      if (!name.includes('[')) {
        out.push(...walkPages(dir, dynamic, child));
      } else if (/^\[.+\]$/.test(name) && !/^\[(?:\.\.\.)?page\]$/.test(name)) {
        // `[slug]/index.astro` is a dynamic route like `[slug].astro`.
        const folder = rel ? `/${rel}/` : '/';
        (name.startsWith('[...') ? dynamic.rest : dynamic.one).push(folder);
      }
      continue;
    }
    const m = PAGE_EXT.exec(name);
    if (!m || !entry.isFile()) continue;
    if (name.includes('[')) {
      // A dynamic route is not a page of its own; note what it can serve.
      // `[page]` is pagination (blog/page/[page].astro), never an entry.
      if (/^\[(?:\.\.\.)?page\]/.test(name)) continue;
      const folder = rel ? `/${rel}/` : '/';
      (name.startsWith('[...') ? dynamic.rest : dynamic.one).push(folder);
      continue;
    }
    const stem = child.slice(0, -m[0].length);
    const path = stem === 'index' ? '/' : `/${stem.replace(/\/index$/, '')}/`;
    out.push({ path, file: join(dir, child), ext: m[1] });
  }
  return out;
}

// Whether a URL is one this site can serve: a page file, or a dynamic route
// covering its folder. An entry whose permalink nothing renders is not listed.
function servable(path: string, pages: Map<string, unknown>, dynamic: DynamicRoutes): boolean {
  if (pages.has(path)) return true;
  const parent = path.slice(0, path.lastIndexOf('/', path.length - 2) + 1);
  return dynamic.one.includes(parent) || dynamic.rest.some((folder) => path.startsWith(folder));
}

// Entries an owner has hidden by any common convention.
// `status` is often a domain field ("open" job, "upcoming" event, "sold"
// listing), so only draft-like values hide an entry.
const HIDDEN_STATUSES = new Set(['draft', 'private', 'unpublished', 'hidden', 'trash', 'deleted']);
const HIDDEN_VISIBILITY = new Set(['private', 'hidden', 'unlisted', 'draft']);

function flag(value: unknown): boolean {
  return value === true || (typeof value === 'string' && value.trim().toLowerCase() === 'true');
}

function entryHidden(data: Record<string, any>): boolean {
  const robots = typeof data.seo?.robots === 'string' ? data.seo.robots.toLowerCase() : '';
  const status = typeof data.status === 'string' ? data.status.trim().toLowerCase() : '';
  const visibility = typeof data.visibility === 'string' ? data.visibility.trim().toLowerCase() : '';
  return (
    // `draft` exactly as the site's own routes read it (`!data.draft`).
    Boolean(data.draft) || flag(data.noindex) || flag(data.hidden) || flag(data.unlisted) ||
    flag(data.seo?.noindex) || data.published === false || data.published === 'false' ||
    /\b(?:noindex|none)\b/.test(robots) || HIDDEN_STATUSES.has(status) || HIDDEN_VISIBILITY.has(visibility)
  );
}

// Paths the site's own navigation links, in order — the pages the business
// itself treats as primary.
function navOrder(root: string): string[] {
  const seen: string[] = [];
  for (const name of ['Header.astro', 'Footer.astro']) {
    const source = readText(join(root, 'src', 'components', name));
    for (const m of source.matchAll(/["'`](\/[^"'`\s#?]*)/g)) {
      const path = m[1].endsWith('/') ? m[1] : `${m[1]}/`;
      if (!seen.includes(path)) seen.push(path);
    }
  }
  return seen;
}

function entryDescription(data: Record<string, any>): string | undefined {
  for (const key of ['description', 'excerpt', 'summary']) {
    if (typeof data[key] === 'string' && data[key].trim()) return data[key];
  }
  if (typeof data.seo?.description === 'string' && data.seo.description.trim()) {
    return data.seo.description;
  }
  const facts = [data.role, data.jobTitle, data.location].filter(
    (v) => typeof v === 'string' && v.trim(),
  );
  return facts.length ? facts.join(', ') : undefined;
}

function entryDate(data: Record<string, any>): Date | undefined {
  const value = data.pubDate ?? data.date ?? data.updatedDate;
  return value instanceof Date ? value : undefined;
}

// Nothing in this file may fail the build: any unexpected error falls back to
// the bare identity, which is still honest.
export async function GET({ site }: APIContext) {
  let body: string;
  try {
    body = await buildLlmsTxt(site);
  } catch {
    let name = '';
    try {
      name = cleanSiteName((await loadSiteConfig()).identity?.name);
    } catch {
      // keep the default
    }
    body = renderLlmsTxt({ name: name && !isPlaceholderName(name) ? name : 'Website', sections: [] });
  }
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
}

// Leading/trailing slashes off, by index: an unanchored `\/+$` regex is
// quadratic on a long run of slashes inside CMS data.
function trimSlashes(text: string): string {
  let lo = 0;
  let hi = text.length;
  while (lo < hi && text[lo] === '/') lo += 1;
  while (hi > lo && text[hi - 1] === '/') hi -= 1;
  return text.slice(lo, hi);
}

// A CMS `permalink` as the site's own [...permalink] catch-all reads it: no
// query or fragment, no `.`/`..` segment, always one leading and trailing
// slash. `//host/x` therefore stays on this site.
function permalinkPath(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length > 2048) return undefined;
  const trimmed = trimSlashes(value.split(/[?#]/)[0]);
  if (!trimmed || trimmed.split('/').some((s) => s === '.' || s === '..' || s === '')) return undefined;
  return `/${trimmed}/`;
}

// User-agent groups whose `Disallow:` rules /llms.txt honours: everyone (`*`)
// and the AI crawlers this file is written for. A rule aimed only at an SEO
// bot (AhrefsBot, SemrushBot) says nothing about answer engines.
const LLM_AGENTS = /^(?:\*|gptbot|chatgpt-user|oai-searchbot|claudebot|claude-web|claude-searchbot|claude-user|anthropic-ai|google-extended|perplexitybot|perplexity-user|ccbot|applebot-extended|meta-externalagent|bytespider|cohere-ai)$/i;

// `Disallow:` paths from the site's robots.txt (static file and route). The
// owner's decision to keep a page out of search covers answer engines too.
// Read line by line, so the route's `'User-agent: *', 'Allow: /'` literals
// parse like the file; comments are skipped. Every rule is kept (see
// compileRules for how they are matched within a bounded cost).
function disallowed(root: string): string[] {
  // public/robots.txt: one directive per line, everything after `#` a comment.
  const fileLines = readText(join(root, 'public', 'robots.txt'))
    .slice(0, 200_000)
    .split(/\r\n|\r|\n/)
    .map((line) => line.split('#')[0]);
  // robots.txt.ts: only its string literals are robots lines (the scaffold's
  // `const lines = ['User-agent: *', 'Allow: /']` and the carried form), so
  // code and `// comments` around them are never read as directives.
  const routeSource = readText(join(root, 'src', 'pages', 'robots.txt.ts'))
    .slice(0, 200_000)
    .split(/\r?\n/)
    .filter((raw) => !/^\s*(?:\/\/|\/\*|\*)/.test(raw))
    .join('\n');
  const routeLines: string[] = [];
  // Quoted strings stop at a line break; a template literal may span lines.
  for (const m of routeSource.matchAll(/'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`/g)) {
    // `\n` inside one literal ('User-agent: *\nDisallow: /x') is a line break.
    const text = (m[1] ?? m[2] ?? m[3] ?? '').replace(/\\([nr])|\\(.)/g, (_, nl, ch) => (nl ? '\n' : ch));
    routeLines.push(...text.split(/\r?\n/));
  }
  const rules: string[] = [];
  for (const lines of [fileLines, routeLines]) {
    let agents: string[] = [];
    let inRules = false;
    for (const line of lines) {
      const m = /^\s*(user-agent|allow|disallow)\s*:\s*(\S*)/i.exec(line.slice(0, 1000));
      if (!m) continue;
      const field = m[1].toLowerCase();
      if (field === 'user-agent') {
        if (inRules) {
          agents = [];
          inRules = false;
        }
        // `GPTBot/1.0` is GPTBot.
        agents.push(m[2].split('/')[0]);
        continue;
      }
      inRules = true;
      const rule = m[2].slice(0, 300);
      if (field === 'disallow' && /^[/*]/.test(rule) && agents.some((a) => LLM_AGENTS.test(a))) {
        rules.push(rule);
      }
    }
  }
  return rules;
}

// Plain rules are prefix/exact matches (linear, any number of them).
// Wildcard rules use the glob walk, at most 200 of them; past that a rule
// fences on its literal prefix before the first `*` — broader, never looser.
function compileRules(raw: string[]): (path: string) => boolean {
  const prefixes: string[] = [];
  const exact = new Set<string>();
  const globs: string[] = [];
  for (const rule of new Set(raw.map(decoded))) {
    const star = rule.indexOf('*');
    if (star < 0) {
      if (rule.endsWith('$')) exact.add(rule.slice(0, -1));
      else prefixes.push(rule);
    } else if (globs.length < 200) {
      globs.push(rule);
    } else {
      prefixes.push(rule.slice(0, star));
    }
  }
  return (path) =>
    exact.has(path) || prefixes.some((p) => path.startsWith(p)) || globs.some((g) => robotsMatch(path, g));
}

function decoded(text: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/** robots.txt matching (prefix, `*` = any run, trailing `$` = anchored), done
 * with the two-pointer glob walk: O(path × rule) per pair, whatever the rule
 * holds.
 * A RegExp built from the rule backtracks exponentially on `*a*a*a…`, and a
 * CPU-bound hang is something no try/catch in this file can stop. */
export function robotsMatch(path: string, rule: string): boolean {
  const anchored = rule.endsWith('$');
  const body = (anchored ? rule.slice(0, -1) : rule).replace(/\*+/g, '*');
  const p = anchored ? body : `${body}*`;
  let i = 0;
  let j = 0;
  let star = -1;
  let mark = 0;
  while (i < path.length) {
    if (j < p.length && p[j] === '*') {
      star = j;
      j += 1;
      mark = i;
    } else if (j < p.length && p[j] === path[i]) {
      i += 1;
      j += 1;
    } else if (star >= 0) {
      j = star + 1;
      mark += 1;
      i = mark;
    } else {
      return false;
    }
  }
  while (j < p.length && p[j] === '*') j += 1;
  return j === p.length;
}

async function buildLlmsTxt(site: URL | undefined): Promise<string> {
  const root = process.cwd();
  const abs = (path: string) => {
    try {
      return site ? new URL(path, site).href : path;
    } catch {
      return path;
    }
  };
  const identity = (await loadSiteConfig()).identity ?? {};
  const fence = compileRules(disallowed(root));
  const isFenced = (path: string) => fence(decoded(path));
  // The migration engine's sidecar: {name: {label, listing?, url_prefix?}}.
  const labels: Record<string, { label?: string; listing?: string; url_prefix?: string }> =
    readJson(join(root, 'src', 'data', 'collections.json')) ?? {};

  // --- pages -----------------------------------------------------------------
  const dynamic: DynamicRoutes = { rest: [], one: [] };
  const pageFiles = walkPages(join(root, 'src', 'pages'), dynamic).filter((p) => !EXCLUDED.test(p.path));
  const shopify = Boolean(
    import.meta.env.PUBLIC_SHOPIFY_DOMAIN && import.meta.env.PUBLIC_SHOPIFY_STOREFRONT_TOKEN,
  );
  const metas = new Map(
    pageFiles.map((p) => {
      const source = readText(p.file);
      return [p.path, { ...pageMeta(source, p.ext, p.path), file: p, shopifyScaffold: source.includes('isShopifyEnabled') }];
    }),
  );
  const home = metas.get('/');

  // A repo without site-config falls back to the home page's own title.
  let name = cleanSiteName(identity.name);
  if (isPlaceholderName(name)) name = cleanSiteName(home?.title);
  if (isPlaceholderName(name)) name = site ? site.hostname.replace(/^www\./, '') : 'Website';
  const descriptionCandidates = [
    identity.description,
    identity.tagline,
    home?.description,
    home ? jsonLdDescription(readText(home.file.file)) : undefined,
  ];
  const description = descriptionCandidates
    .map((d) => oneLine(d, 500))
    .find((d) => d && !isPlaceholderDescription(d));
  const tagline = oneLine(identity.tagline, 200);

  // --- collections -------------------------------------------------------------
  const claimed = new Set<string>();
  const collections: LlmsCollection[] = [];
  for (const collection of await loadCollectionNames(root)) {
    if (PAGE_COLLECTIONS.has(collection)) continue;
    let rows: Array<{ id: string; data: Record<string, any> }>;
    try {
      rows = (await getCollection(collection as any)) as any;
    } catch {
      continue;
    }
    const prefix =
      collection === 'blog'
        ? 'blog'
        : typeof labels[collection]?.url_prefix === 'string'
          ? trimSlashes(labels[collection].url_prefix!.slice(0, 300))
          : '';
    // A dynamic route directly under the prefix (file or directory form).
    const routed =
      Boolean(prefix) && (dynamic.one.includes(`/${prefix}/`) || dynamic.rest.includes(`/${prefix}/`));
    const entries: LlmsEntry[] = [];
    const paths: string[] = [];
    for (const row of rows) {
      const data = row.data ?? {};
      if (entryHidden(data) || row.id.startsWith('_')) continue;
      let path: string | undefined;
      if (data.permalink != null) path = permalinkPath(data.permalink);
      else if (routed) path = permalinkPath(`${prefix}/${row.id}`);
      if (!path || !servable(path, metas, dynamic)) continue;
      // The entry's page (when it has its own file) carries the noindex.
      if (metas.get(path)?.noindex || isFenced(path)) continue;
      claimed.add(path);
      paths.push(path);
      entries.push({
        title: oneLine(data.title ?? data.name, 300) || humanize(row.id),
        url: abs(path),
        description: entryDescription(data),
        date: entryDate(data),
      });
    }
    if (!entries.length) continue;
    // The collection's index page: the listing the engine recorded, else its
    // declared prefix, else the one folder every entry shares — whichever is
    // a page on this site.
    const folders = new Set(paths.map((p) => p.split('/')[1] ?? ''));
    const indexFolder = prefix || (folders.size === 1 ? [...folders][0] : '');
    const listing = labels[collection]?.listing;
    const indexPath = [
      typeof listing === 'string' && listing.startsWith('/') ? `${listing.replace(/\/+$/, '')}/` : '',
      indexFolder ? `/${indexFolder}/` : '',
    ].find((p) => p && metas.has(p));
    collections.push({
      label: oneLine(labels[collection]?.label, 80) || humanize(collection),
      indexUrl: indexPath ? abs(indexPath) : undefined,
      entries,
    });
  }

  // --- assemble ----------------------------------------------------------------
  const nav = navOrder(root);
  const rank = (path: string) => {
    if (path === '/') return -1;
    const at = nav.indexOf(path);
    return at < 0 ? nav.length + path.split('/').length * 1000 : at;
  };
  const pages: LlmsLink[] = [...metas.entries()]
    .filter(([path, meta]) => !meta.noindex && !claimed.has(path) && !isFenced(path))
    // The Shopify scaffold's own catalog is a placeholder until a store is
    // connected; a site's own /products/ pages are listed like any other.
    .filter(([path, meta]) => shopify || path !== '/products/' || !meta.shopifyScaffold)
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .slice(0, LLMS_LIMITS.pages)
    .map(([path, meta]) => {
      // The home page is "Home": its <title> is the brand or a tagline.
      const fallback = humanize(path.split('/').filter(Boolean).pop() ?? '');
      const title = path === '/' ? 'Home' : meta.title ? stripSiteAffix(meta.title, name) : fallback;
      const raw = oneLine(meta.description, 500);
      const note = raw && !isPlaceholderDescription(raw) ? raw : '';
      return {
        title,
        url: abs(path),
        // The home page's description is usually the summary above it.
        description: note && note !== description ? note : undefined,
      };
    });

  const sections: LlmsSection[] = [];
  if (pages.length) sections.push({ heading: 'Pages', links: pages });
  for (const collection of collections.slice(0, LLMS_LIMITS.collections)) {
    sections.push(collectionSection(collection));
  }
  const pkg = readJson(join(root, 'package.json')) ?? {};
  const deps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
  if (site && '@astrojs/sitemap' in deps) {
    sections.push({
      heading: 'Optional',
      links: [{ title: 'Sitemap', url: abs('/sitemap.xml'), description: 'every indexable page' }],
    });
  }

  return renderLlmsTxt({
    name,
    description,
    details: tagline && tagline !== description ? tagline : undefined,
    sections,
  });
}
