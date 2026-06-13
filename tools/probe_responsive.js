(() => {
  const out = [];
  out.push('innerWidth=' + window.innerWidth + ' innerHeight=' + window.innerHeight);
  // 1) Is there already a docked chat container in this (roster) window?
  const dockHints = ['chatWindow','ChatRoomGroup','chatDialogs','multiChatDialog','chatBody','OpenChat','chatColumn'];
  dockHints.forEach(h => {
    const n = document.querySelectorAll('[class*="' + h + '"]').length;
    if (n) out.push('PRESENT in roster: [class*="' + h + '"] x' + n);
  });
  // 2) Scan stylesheets for rules mentioning "responsive" or width breakpoints
  //    that toggle a combined layout.
  const hits = [];
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { continue; }
    if (!rules) continue;
    const scan = (rl, ctx) => {
      for (const r of rl) {
        if (r.media && r.cssRules) {            // @media block
          scan(r.cssRules, '@media ' + r.media.mediaText);
        } else if (r.selectorText && /responsive|combined|docked|narrow|wide/i.test(r.selectorText)) {
          hits.push((ctx ? ctx + '  ' : '') + r.selectorText + ' { ' +
            (r.style && r.style.cssText ? r.style.cssText.slice(0, 80) : '') + ' }');
        }
      }
    };
    scan(rules, '');
  }
  out.push('--- responsive/breakpoint rules (' + hits.length + ') ---');
  out.push(...hits.slice(0, 60));
  return out.join('\n');
})()
