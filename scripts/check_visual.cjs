const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.argv[2]);
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.argv[3]});
  const page = await browser.newPage({viewport:{width:736,height:850}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(pathToFileURL(path.resolve('artifacts/tier1-preview.html')).href);
  const frame = page.frameLocator('iframe');
  await frame.locator('#betbot-count').waitFor();
  const values = {};
  for (const value of [50,55,60,80]) {
    await frame.locator('#betbot-threshold').evaluate((element,v) => {element.value=v; element.dispatchEvent(new Event('input',{bubbles:true}));},value);
    values[value] = {accuracy:await frame.locator('#betbot-accuracy').innerText(),count:await frame.locator('#betbot-count').innerText(),coverage:await frame.locator('#betbot-coverage').innerText()};
  }
  const report = JSON.parse(fs.readFileSync('artifacts/draft-comparison/classic/report.json','utf8'));
  if (Number(values[50].count) !== report.all_maps.count || Number(values[55].count) !== report.signals.count) throw new Error('Rendered data differs from report');
  await frame.locator('#betbot-threshold').evaluate(element => {element.value=55;element.dispatchEvent(new Event('input',{bubbles:true}));});
  for (const width of [736,360]) {
    await page.setViewportSize({width,height:850});
    const overflow = await frame.locator('#betbot-tier1-history').evaluate(element=>element.scrollWidth>element.clientWidth+1);
    if (overflow) throw new Error('Horizontal overflow at '+width);
    await page.screenshot({path:'artifacts/tier1-'+width+'.png',fullPage:true});
  }
  await browser.close();
  if (errors.length) throw new Error(errors.join('\n'));
  fs.writeFileSync('artifacts/visual-check.json',JSON.stringify(values,null,2));
  console.log(JSON.stringify({values,pageErrors:errors.length}));
})().catch(error => {console.error(error);process.exitCode=1;});
