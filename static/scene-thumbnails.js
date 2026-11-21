(() => {
  'use strict';

  function valid(url) {
    return typeof url === 'string' && (
      /^\/api\/[\w/-]+\/thumbnail(?:\/[\w-]+)?$/.test(url) ||
      /^https:\/\/sentinel-cogs\.s3\.us-west-2\.amazonaws\.com\/sentinel-s2-l2a-cogs\/[\w/-]+\/preview\.jpg$/.test(url)
    );
  }

  function create(url, label) {
    const box = document.createElement('div');
    box.className = 'scene-catalog-thumbnail';
    if (!valid(url)) {
      box.textContent = 'Chưa có ảnh xem trước';
      return box;
    }
    const image = document.createElement('img');
    image.alt = `Ảnh xem trước · ${label || 'cảnh vệ tinh'}`;
    image.width = 80;
    image.height = 80;
    image.loading = 'lazy';
    image.decoding = 'async';
    image.referrerPolicy = 'no-referrer';
    const loading = document.createElement('span');
    loading.textContent = 'Đang tải…';
    image.style.opacity = '0';
    image.addEventListener('load', () => {
      loading.remove();
      image.style.opacity = '';
    });
    image.addEventListener('error', () => { box.textContent = 'Ảnh xem trước không khả dụng'; });
    if (url.startsWith('/api/s5p/search/')) {
      image.addEventListener('load', () => {
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const pixels = canvas.getContext('2d');
        if (!pixels) return;
        pixels.drawImage(image, 0, 0);
        const rgba = pixels.getImageData(0, 0, canvas.width, canvas.height).data;
        if (!rgba.some((value, index) => index % 4 === 3 && value > 0)) {
          box.textContent = 'Không có pixel hợp lệ trong ROI';
        }
      });
    }
    image.src = url;
    box.append(image, loading);
    return box;
  }

  function append(row, scene, label, className = 'scene-row-with-thumbnail') {
    row.classList.add(className);
    row.append(create(scene.thumbnail_url, label));
  }

  window.SceneThumbnails = { valid, create, append };
})();
