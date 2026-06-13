(() => {
  // Build an indented structural skeleton: tag + semantic class prefixes.
  // Steam CSS-module classes look like "ClassName_HASH" or "_HASH"; keep the
  // readable part so we can derive [class*="prefix"] selectors.
  const prefix = (cls) => {
    if (!cls) return '';
    return cls.split(/\s+/).map(c => {
      // strip a trailing _<hash> or leading _<hash>
      const m = c.match(/^([A-Za-z][A-Za-z0-9]+?)_[A-Za-z0-9_-]{4,}$/);
      if (m) return m[1];
      if (/^_[A-Za-z0-9-]{4,}$/.test(c)) return null; // pure-hash, useless
      return c;
    }).filter(Boolean).join('.');
  };
  const out = [];
  let count = 0;
  const MAX = 600;
  const walk = (el, depth) => {
    if (count++ > MAX) return;
    const tag = el.tagName.toLowerCase();
    const p = prefix(el.getAttribute('class'));
    const aria = el.getAttribute('aria-label');
    const title = el.getAttribute('title');
    const type = el.getAttribute('type');
    const ph = el.getAttribute('placeholder');
    let extra = '';
    if (aria) extra += ` aria="${aria}"`;
    if (title) extra += ` title="${title}"`;
    if (type) extra += ` type=${type}`;
    if (ph) extra += ` ph="${ph}"`;
    // short text for leaf-ish nodes
    if (el.children.length === 0 && el.textContent && el.textContent.trim().length <= 30)
      extra += ` txt="${el.textContent.trim()}"`;
    out.push('  '.repeat(depth) + tag + (p ? '.' + p : '') + extra);
    for (const c of el.children) walk(c, depth + 1);
  };
  walk(document.body, 0);
  return out.join('\n') + `\n--- nodes: ${count} (capped ${MAX}) ---`;
})()
