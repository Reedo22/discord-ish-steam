(() => {
  const out = [];
  // current classes on html/body that gate layout
  out.push('html.class = ' + document.documentElement.className);
  out.push('body.class = ' + document.body.className);
  out.push('.singlewindow present? ' + document.querySelectorAll('.singlewindow').length);
  out.push('.multiplewindows present? ' + document.querySelectorAll('.multiplewindows,[class*="multiplewindow"]').length);
  // localStorage keys that look layout/window related
  out.push('--- localStorage keys (filtered) ---');
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (/window|chat|friend|layout|combine|single|dock|setting/i.test(k)) {
        let v = localStorage.getItem(k);
        if (v && v.length > 120) v = v.slice(0, 120) + '…';
        out.push(k + ' = ' + v);
      }
    }
  } catch (e) { out.push('localStorage err: ' + e); }
  // Steam friends settings global, if exposed
  out.push('--- globals ---');
  ['g_FriendsUIApp','FriendsUIStore','g_PopupManager','SteamUIStore'].forEach(g => {
    out.push(g + ': ' + (typeof window[g]));
  });
  return out.join('\n');
})()
