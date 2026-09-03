import type { APIContext } from 'astro';

// AUTO-GENERATED (Willo). Serves /robots.txt.
// `Allow: /` is deliberately blanket — the noindex routes are kept out of the
// sitemap, not out of the crawl. Disallowing a page hides its noindex tag.
export const prerender = true;

export function GET({ site }: APIContext) {
  const lines = ['User-agent: *', 'Allow: /'];
  // `site` comes from astro.config.mjs and tracks the live canonical URL.
  // Absent only before the first canonical is baked in — emit no directive
  // then rather than a relative one, which crawlers ignore.
  if (site) {
    lines.push('', `Sitemap: ${new URL('/sitemap.xml', site).href}`);
  }
  return new Response(`${lines.join('\n')}\n`, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}
