// Drives the real customer UI through one cancellation: message -> proposal -> confirm -> status shown as cancelled.
// Same selectors as frontend/tests/live/real-backend.spec.ts. Started by run.py, which sets the environment below.
//
//   usage: node capture.cjs <workDir> <screenshotPath>
//   env:   WEB_URL, ORDER_ID, CUSTOMER_EMAIL, CUSTOMER_PASSWORD
//
// Writes the video into <workDir>, the screenshot to <screenshotPath> and the event times (seconds from the start of
// the recording) to <workDir>/marks.json, which run.py uses to cut the GIF.
const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');

const [workDir, screenshotPath] = process.argv.slice(2);
const { WEB_URL, ORDER_ID, CUSTOMER_EMAIL, CUSTOMER_PASSWORD } = process.env;
const MESSAGE = `Tôi muốn huỷ đơn hàng ${ORDER_ID}`;
const VIEWPORT = { width: 1200, height: 1000 };

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    locale: 'vi-VN',
    timezoneId: 'Asia/Ho_Chi_Minh',
    recordVideo: { dir: workDir, size: VIEWPORT },
  });
  const started = Date.now();
  const marks = {};
  const mark = (name) => {
    marks[name] = (Date.now() - started) / 1000;
  };
  const page = await context.newPage();

  try {
    await page.goto(WEB_URL);
    await page.locator('button.login').click();
    await page.locator('#login-email').fill(CUSTOMER_EMAIL);
    await page.locator('#login-password').fill(CUSTOMER_PASSWORD);
    // Signing in starts a new conversation owned by the customer; chat only after it exists, or the message goes to
    // the earlier guest conversation.
    const conversation = page.waitForResponse((r) => r.request().method() === 'POST' && /\/conversations$/.test(r.url()));
    await page.getByRole('dialog').locator('button[type=submit]').click();
    await conversation;
    await page.locator('#login-email').waitFor({ state: 'detached' });

    const input = page.getByPlaceholder('Nhập câu hỏi hoặc yêu cầu hỗ trợ...');
    await input.waitFor();
    await page.waitForTimeout(900);

    mark('typeStart');
    await input.click();
    await input.pressSequentially(MESSAGE, { delay: 20 });
    mark('typed');
    await page.waitForTimeout(250);
    await input.press('Enter');
    mark('sent');

    // The proposal: the card with Confirm and Cancel, for this order. Nothing has changed yet.
    const confirm = page.getByRole('button', { name: 'Xác nhận', exact: true });
    await page.getByText(`Xác nhận hủy đơn hàng ${ORDER_ID}`).waitFor({ timeout: 120000 });
    await confirm.waitFor();
    await page.getByRole('button', { name: 'Hủy', exact: true }).waitFor();
    await page.locator('.typing').waitFor({ state: 'detached', timeout: 10000 }); // "đang suy nghĩ..." is gone: the reply is complete
    // The chat scrolls down before the card has its final height, so a longer reply can leave the card's lower edge
    // under the input bar. Bring the whole card into view; the reply length varies from run to run.
    await page.locator('.cardwrap').last().evaluate((card) => card.scrollIntoView({ block: 'end' }));
    await page.waitForTimeout(1200);
    mark('proposalVisible');
    await page.screenshot({ path: screenshotPath });

    // Let the viewer read the card, then confirm.
    await page.waitForTimeout(1900);
    mark('confirmClicked');
    await confirm.click();
    await page.getByText('Đã xác nhận thành công!').waitFor({ timeout: 30000 });
    mark('confirmedCard');
    // The app does not refresh the order badge in place after a confirmation (it clears its order cache for the next
    // load), so the status is shown the way a customer would see it: reload the page, which reads the order again.
    await page.waitForTimeout(700);
    mark('reload');
    await page.reload();
    await page.getByText('Đã hủy', { exact: true }).first().waitFor({ timeout: 15000 });
    mark('cancelledVisible');
    await page.waitForTimeout(1600);
    mark('end');
  } catch (error) {
    await page.screenshot({ path: path.join(workDir, 'failure.png') }).catch(() => {});
    fs.writeFileSync(path.join(workDir, 'marks.json'), JSON.stringify({ ...marks, failed: String(error).split('\n')[0] }));
    await context.close();
    await browser.close();
    console.error('capture failed:', String(error).split('\n')[0]);
    process.exit(1);
  }

  const video = page.video();
  await context.close(); // flushes the video file
  fs.writeFileSync(path.join(workDir, 'marks.json'), JSON.stringify({ ...marks, video: await video.path() }));
  await browser.close();
  console.log('captured', JSON.stringify(marks));
})();
