/* Opcje sa pobierane po sprawdzeniu dostepu przez serwer, bez danych innych firm. */
(() => {
  const form = document.querySelector('[data-transfer-form]');
  if (!form) return;
  const company = form.querySelector('[data-transfer-company]');
  const contact = form.querySelector('[data-transfer-contact]');
  const technician = form.querySelector('[data-transfer-technician]');
  const next = form.querySelector('[data-transfer-next]');
  const result = form.querySelector('[data-transfer-result]');
  let activeRequest;
  company.addEventListener('change', async () => {
    if (activeRequest) activeRequest.abort();
    const controller = new AbortController();
    activeRequest = controller;
    contact.replaceChildren(new Option('Bez kontaktu', ''));
    technician.replaceChildren(new Option('Nieprzypisany', ''));
    contact.disabled = technician.disabled = next.disabled = true;
    result.textContent = company.value ? 'Wczytywanie osób…' : 'Wybierz firmę docelową.';
    if (!company.value) return;
    try {
      const url = new URL(company.dataset.optionsUrl, window.location.origin);
      url.searchParams.set('tenant_id', company.value);
      const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('Nie udało się wczytać osób. Sprawdź dostęp do firmy i wybierz ją ponownie.');
      const data = await response.json();
      if (controller !== activeRequest) return;
      data.contacts.forEach(person => contact.add(new Option(`${person.name} · ${person.email}`, person.id)));
      data.technicians.forEach(person => technician.add(new Option(person.name, person.id)));
      contact.disabled = technician.disabled = next.disabled = false;
      result.textContent = data.contacts.length ? 'Lista osób została zaktualizowana.' : 'Firma nie ma kontaktów z adresem e-mail. Możesz przenieść zgłoszenie bez kontaktu.';
    } catch (error) {
      if (error.name !== 'AbortError' && controller === activeRequest) result.textContent = error.message;
    }
  });
})();
