const puppeteer = require('puppeteer');

(async () => {
    const browser = await puppeteer.launch({
        headless: 'new',
        args: ['--no-sandbox'],
        executablePath: 'C:\\Users\\hehehe\\.cache\\puppeteer\\chrome\\win64-150.0.7871.24\\chrome-win64\\chrome.exe',
    });
    const page = await browser.newPage();

    await page.setViewport({ width: 1440, height: 900 });
    await page.goto('http://localhost:3456', { waitUntil: 'networkidle0', timeout: 60000 });
    await new Promise((r) => setTimeout(r, 1200));
    await page.screenshot({ path: '.design-review/hero-desktop.png' });

    await page.click('#faq-trigger-1');
    await new Promise((r) => setTimeout(r, 700));
    const faq = await page.$('.faq');
    await faq.screenshot({ path: '.design-review/faq-open.png' });

    await page.setViewport({ width: 390, height: 844 });
    await page.reload({ waitUntil: 'networkidle0' });
    await new Promise((r) => setTimeout(r, 1000));
    await page.click('.nav__burger');
    await new Promise((r) => setTimeout(r, 500));
    await page.screenshot({ path: '.design-review/mobile-menu.png' });

    await browser.close();
    console.log('interaction shots done');
})();
