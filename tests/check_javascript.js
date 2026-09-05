const fs = require('fs');

for (const path of [
  'extension/background.js',
  'extension/content.js',
  'extension/popup/popup.js'
]) {
  new Function(fs.readFileSync(path, 'utf8'));
}

const dashboard = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const scripts = [...dashboard.matchAll(/<script>([\s\S]*?)<\/script>/gi)];
if (scripts.length !== 1) {
  throw new Error(`Expected one inline dashboard script, found ${scripts.length}`);
}
new Function(scripts[0][1]);
console.log('JavaScript syntax: PASS');
