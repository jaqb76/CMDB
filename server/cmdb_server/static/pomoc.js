(function(){
  const search = document.getElementById('search');
  const sections = [...document.querySelectorAll('[data-search], section.guide')];
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    sections.forEach(s => {
      const hay = (s.getAttribute('data-search') || '') + ' ' + s.innerText;
      s.classList.toggle('hidden', q && !hay.toLowerCase().includes(q));
    });
  });

  const links = [...document.querySelectorAll('#toc a')];
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if(e.isIntersecting){
        links.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + e.target.id));
      }
    });
  }, {rootMargin:'-20% 0px -70% 0px'});
  document.querySelectorAll('main [id]').forEach(el => obs.observe(el));

  const checks = [...document.querySelectorAll('#checks input')];
  const progress = document.getElementById('progress');
  function updateProgress(){
    const n = checks.filter(c => c.checked).length;
    progress.textContent = 'Postęp: ' + n + ' / ' + checks.length + (n === checks.length ? ' — szkolenie ukończone ✓' : '');
    progress.className = 'callout ' + (n === checks.length ? 'ok' : '');
  }
  checks.forEach(c => c.addEventListener('change', updateProgress));
  updateProgress();
})();

