const puppeteer = require('puppeteer');

(async () => {
    const browser = await puppeteer.launch({
        headless: 'new',
        args: ['--no-sandbox'],
        executablePath: 'C:\\Users\\hehehe\\.cache\\puppeteer\\chrome\\win64-150.0.7871.24\\chrome-win64\\chrome.exe',
    });
    const page = await browser.newPage();

    const autoScroll = async () => {
        await page.evaluate(async () => {
            await new Promise((resolve) => {
                let y = 0;
                const step = () => {
                    y += 400;
                    window.scrollTo(0, y);
                    if (y < document.body.scrollHeight + 800) {
                        setTimeout(step, 120);
                    } else {
                        window.scrollTo(0, 0);
                        setTimeout(resolve, 900);
                    }
                };
                step();
            });
        });
    };

    await page.setViewport({ width: 1440, height: 900 });
    await page.goto('http://localhost:3456', { waitUntil: 'networkidle0', timeout: 60000 });
    await new Promise((r) => setTimeout(r, 1500));
    await autoScroll();
    await page.screenshot({ path: '.design-review/landing-desktop.png', fullPage: true });

    await page.setViewport({ width: 390, height: 844 });
    await page.reload({ waitUntil: 'networkidle0' });
    await new Promise((r) => setTimeout(r, 1500));
    await autoScroll();
    await page.screenshot({ path: '.design-review/landing-mobile.png', fullPage: true });

    await browser.close();
    console.log('screenshots done');
})();
