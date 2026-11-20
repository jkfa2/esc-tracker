(function () {
  if (window.__pickerActive) { return; }
  window.__pickerActive = true;
  var BAD_CLASS = /^(active|open|opened|show|hidden|hover|focus|selected|current|is-|has-|js-|ng-|css-|sc-|jsx-|elementor-element-)|\d{3,}|^[a-z0-9]{6,}$/i;
  var box = document.createElement('div');
  box.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;border:2px solid #e11d48;background:rgba(225,29,72,.15);transition:all .05s';
  var bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;z-index:2147483647;left:0;right:0;bottom:0;padding:8px 12px;font:13px/1.4 monospace;background:#111;color:#fff;pointer-events:none';
  bar.textContent = 'Click the element to monitor (Esc to cancel)';
  document.body.appendChild(box);
  document.body.appendChild(bar);
  var current = null;

  function esc(s) { return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1'); }
  function count(sel) { try { return document.querySelectorAll(sel).length; } catch (e) { return 0; } }

  function part(el) {
    var tag = el.tagName.toLowerCase();
    if (el.id && !/\d{4,}/.test(el.id) && count('#' + esc(el.id)) === 1) { return '#' + esc(el.id); }
    var classes = Array.prototype.slice.call(el.classList).filter(function (c) { return !BAD_CLASS.test(c); }).slice(0, 2);
    var s = tag + classes.map(function (c) { return '.' + esc(c); }).join('');
    var parent = el.parentElement;
    if (parent) {
      var same = Array.prototype.filter.call(parent.children, function (n) { return n.matches(s); });
      if (same.length > 1) { s += ':nth-of-type(' + (Array.prototype.indexOf.call(parent.children, el) + 1) + ')'; if (!classes.length) { s = tag + ':nth-child(' + (Array.prototype.indexOf.call(parent.children, el) + 1) + ')'; } }
    }
    return s;
  }

  function selectorFor(el) {
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && node !== document.body && parts.length < 7) {
      var p = part(node);
      parts.unshift(p);
      var sel = parts.join(' > ');
      if (p.charAt(0) === '#' || count(sel) === 1) { return sel; }
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  function move(e) {
    current = e.target;
    var r = current.getBoundingClientRect();
    box.style.left = r.left + 'px'; box.style.top = r.top + 'px';
    box.style.width = r.width + 'px'; box.style.height = r.height + 'px';
    bar.textContent = selectorFor(current);
  }

  function stop() {
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('click', pick, true);
    document.removeEventListener('keydown', key, true);
    box.remove(); bar.remove();
    window.__pickerActive = false;
  }

  function pick(e) {
    e.preventDefault(); e.stopPropagation();
    var sel = selectorFor(current);
    var n = count(sel);
    stop();
    var done = function () { alert('Selector copied:\n\n' + sel + '\n\nMatches ' + n + ' element(s) on this page.' + (n !== 1 ? '\n(Fine for lists; otherwise pick a more specific element.)' : '')); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(sel).then(done, function () { prompt('Copy this selector:', sel); });
    } else { prompt('Copy this selector:', sel); }
  }

  function key(e) { if (e.key === 'Escape') { stop(); } }

  document.addEventListener('mousemove', move, true);
  document.addEventListener('click', pick, true);
  document.addEventListener('keydown', key, true);
})();
