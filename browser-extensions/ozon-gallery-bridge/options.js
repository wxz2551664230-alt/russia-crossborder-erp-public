document.querySelector('#settings').addEventListener('submit', async event => {
  event.preventDefault();
  await chrome.storage.local.set({erpAccessToken: document.querySelector('#token').value.trim()});
  document.querySelector('#token').value = '';
  document.querySelector('#status').textContent = '已保存';
});
