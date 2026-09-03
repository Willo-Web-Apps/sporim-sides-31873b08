#!/usr/bin/env node
// AUTO-GENERATED sitemap alias (Willo SEO).
// Runs as an npm postbuild hook: republishes the sitemap @astrojs/sitemap just
// built at the conventional /sitemap.xml address, which the integration itself
// cannot emit (it always names its index `sitemap-index.xml`).
// Best-effort by contract — this script must NEVER fail the build.
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

try {
  const dist = join(process.cwd(), 'dist');
  const index = join(dist, 'sitemap-index.xml');
  if (existsSync(index)) {
    const shards = readdirSync(dist).filter((f) => /^sitemap-\d+\.xml$/.test(f));
    // One shard is the normal case — @astrojs/sitemap only splits past 45,000
    // URLs. Publish that flat <urlset> directly so /sitemap.xml IS the sitemap
    // and a crawler needs one fetch instead of two. Past the split there is no
    // single shard to publish, so serve the index; copying shard 0 would
    // silently drop every URL in the others.
    const source = shards.length === 1 ? join(dist, shards[0]) : index;
    writeFileSync(join(dist, 'sitemap.xml'), readFileSync(source));
  }
} catch {
  // Never fail the build.
}
process.exit(0);
