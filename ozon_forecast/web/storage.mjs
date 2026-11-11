// Separate from the service worker: stores only the last validated public report.
let database;
function open() {
  if (!database) database = new Promise((resolve, reject) => {
    const request = indexedDB.open('varanez-reports', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('reports');
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('Хранилище занято другой вкладкой'));
    request.onsuccess = () => { request.result.onversionchange = () => { request.result.close(); database = null; }; resolve(request.result); };
  });
  return database;
}
export async function readReport(kind) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('reports', 'readonly');
    const request = tx.objectStore('reports').get(kind);
    request.onsuccess = () => resolve(request.result ?? null);
    request.onerror = () => reject(request.error);
  });
}
export async function writeReport(kind, report) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('reports', 'readwrite');
    tx.objectStore('reports').put({report, saved_at: new Date().toISOString()}, kind);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}
