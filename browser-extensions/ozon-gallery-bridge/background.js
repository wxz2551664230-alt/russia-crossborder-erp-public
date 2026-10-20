chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== 'OZON_GALLERY_CAPTURE') return false;
  chrome.storage.local.get('erpAccessToken').then(({erpAccessToken}) => {
    if (!erpAccessToken) throw new Error('请在扩展选项中设置 ERP 访问密钥');
    return fetch('http://127.0.0.1:8788/api/v1/ozon-gallery/capture', {
    method: 'POST', headers: {'Content-Type':'application/json','X-Ozon-Gallery-Bridge':'ozon-gallery-bridge-v1','Authorization':`Bearer ${erpAccessToken}`}, body: JSON.stringify(message.payload || {})
  }); }).then(async response => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    if (sender.tab?.id) {
      chrome.action.setBadgeBackgroundColor({tabId:sender.tab.id,color:'#16803c'});
      chrome.action.setBadgeText({tabId:sender.tab.id,text:String(data.image_count || '✓')});
      chrome.action.setTitle({tabId:sender.tab.id,title:`已回传 ${data.image_count || 0} 张Ozon图片`});
      if (message.payload?.auto_close === true) setTimeout(() => chrome.tabs.remove(sender.tab.id, () => void chrome.runtime.lastError), 800);
    }
    sendResponse({ok:true,...data});
  }).catch(error => {
    if (sender.tab?.id) {chrome.action.setBadgeBackgroundColor({tabId:sender.tab.id,color:'#c53030'});chrome.action.setBadgeText({tabId:sender.tab.id,text:'!'});chrome.action.setTitle({tabId:sender.tab.id,title:`Ozon图库回传失败：${String(error.message||error)}`});}
    console.error('OZON_GALLERY_CAPTURE failed', error, message.payload);
    sendResponse({ok:false,error:String(error.message || error)});
  });
  return true;
});
