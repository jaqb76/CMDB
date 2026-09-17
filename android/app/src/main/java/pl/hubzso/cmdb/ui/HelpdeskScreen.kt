package pl.hubzso.cmdb.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.Send
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.AttachFile
import androidx.compose.material.icons.outlined.ChevronRight
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.ConfirmationNumber
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.HourglassEmpty
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.MailOutline
import androidx.compose.material.icons.outlined.Person
import androidx.compose.material.icons.outlined.PhotoCamera
import androidx.compose.material.icons.outlined.QuestionAnswer
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.SupervisorAccount
import androidx.compose.material.icons.outlined.Timer
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import pl.hubzso.cmdb.data.HelpdeskCatalog
import pl.hubzso.cmdb.data.NewMessage
import pl.hubzso.cmdb.data.ReportOption
import pl.hubzso.cmdb.data.NewTicket
import pl.hubzso.cmdb.data.Ticket
import pl.hubzso.cmdb.data.TicketAsset
import pl.hubzso.cmdb.data.TicketAttachment
import pl.hubzso.cmdb.data.TicketDetail
import pl.hubzso.cmdb.data.TicketEntry
import pl.hubzso.cmdb.data.WybranyPlik
import pl.hubzso.cmdb.data.adresNaZdjecie
import pl.hubzso.cmdb.data.opisPliku
import pl.hubzso.cmdb.data.opisRozmiaru

/**
 * Ekrany helpdesku: lista zgloszen, karta, rozmowa i formularz nowego
 * zgloszenia.
 *
 * Dwie rzeczy z projektu graficznego nie maja odpowiednika w danych i nie
 * udajemy, ze maja: priorytet zgloszenia i termin SLA. CMDB ich nie zna -
 * zgloszenie ma status, rodzaj i historie wiadomosci. W miejscu priorytetu
 * pokazujemy rodzaj sprawy, a w miejscu licznika SLA to, co system naprawde
 * wie: od kiedy sprawa czeka na NASZA odpowiedz (ostatnie slowo nalezy do
 * klienta). Wymyslony licznik wygladalby jak zobowiazanie, ktorego nikt nie
 * podjal.
 */
internal enum class HelpdeskView { LIST, TICKET, THREAD, NEW }

// Zakresy listy - te same klucze rozumie serwer.
private val ZAKRESY = listOf(
    "open" to "Otwarte",
    "mine" to "Moje",
    "waiting" to "Czekają",
    "unassigned" to "Nieprzypisane",
    "closed" to "Zamknięte",
)

@Composable private fun kolorStatusu(status: String): Color {
    val kolory = LocalCmdbColors.current
    return when (status) {
        "nowe" -> MaterialTheme.colorScheme.primary
        "w_trakcie" -> kolory.ok
        "oczekuje" -> kolory.warn
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }
}

// --- lista zgloszen ---------------------------------------------------------

@Composable internal fun HelpdeskListScreen(
    state: HelpdeskState,
    padding: PaddingValues,
    focusSearch: Int,
    onOpen: (String) -> Unit,
    onSearch: (String, String) -> Unit,
    onMore: () -> Unit,
    onNew: () -> Unit,
) {
    val kolory = LocalCmdbColors.current
    val lista = rememberLazyListState()
    val fokus = remember { FocusRequester() }
    var obsluzone by remember { mutableIntStateOf(focusSearch) }
    LaunchedEffect(focusSearch) {
        if (focusSearch != obsluzone) {
            obsluzone = focusSearch
            lista.scrollToItem(0)
            runCatching { fokus.requestFocus() }
        }
    }
    // Nazwa firmy przy zgloszeniu ma sens tylko wtedy, gdy konto obsluguje
    // ich kilka - przy jednej byloby to to samo slowo w kazdym wierszu.
    val wieleFirm = (state.catalog?.tenants?.size ?: 0) > 1

    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        state = lista,
        contentPadding = PaddingValues(start = 14.dp, end = 14.dp, top = 12.dp, bottom = 20.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            OutlinedTextField(
                value = state.query,
                onValueChange = { onSearch(it, state.scope) },
                modifier = Modifier.fillMaxWidth().focusRequester(fokus),
                placeholder = { Text("Szukaj numeru, tematu lub adresu", fontSize = 14.sp) },
                leadingIcon = { Icon(Icons.Outlined.Search, null, Modifier.size(20.dp), MaterialTheme.colorScheme.onSurfaceVariant) },
                singleLine = true,
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedContainerColor = MaterialTheme.colorScheme.surface,
                    unfocusedContainerColor = MaterialTheme.colorScheme.surface,
                    focusedBorderColor = MaterialTheme.colorScheme.primary,
                    unfocusedBorderColor = kolory.cardBorder,
                ),
            )
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                LicznikZgloszen("Otwarte", state.counters.open, MaterialTheme.colorScheme.onSurface,
                    state.scope == "open", Modifier.weight(1f)) { onSearch(state.query, "open") }
                LicznikZgloszen("Moje", state.counters.mine, MaterialTheme.colorScheme.primary,
                    state.scope == "mine", Modifier.weight(1f)) { onSearch(state.query, "mine") }
                LicznikZgloszen("Czekają", state.counters.waiting, kolory.danger,
                    state.scope == "waiting", Modifier.weight(1f)) { onSearch(state.query, "waiting") }
            }
        }
        item {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(ZAKRESY, key = { it.first }) { (klucz, etykieta) ->
                    CmdbFilterChip(etykieta, state.scope == klucz) { onSearch(state.query, klucz) }
                }
            }
        }
        items(state.tickets, key = { it.id }) { ticket ->
            KartaZgloszenia(ticket, wieleFirm) { onOpen(ticket.id) }
        }
        if (state.loading) item {
            Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        } else if (state.tickets.isEmpty()) item {
            EmptyState(
                if (state.query.isBlank()) "Brak zgłoszeń w tym widoku"
                else "Żadne zgłoszenie nie pasuje do wyszukiwania",
            )
        }
        if (state.tickets.size < state.total) item {
            OutlinedButton(onClick = onMore, enabled = !state.loading, modifier = Modifier.fillMaxWidth()) {
                Text("Załaduj kolejne (${state.tickets.size} z ${state.total})")
            }
        }
        item {
            Button(onClick = onNew, modifier = Modifier.fillMaxWidth().height(50.dp), shape = RoundedCornerShape(12.dp)) {
                Icon(Icons.Outlined.Add, null)
                Spacer(Modifier.width(8.dp))
                Text("Nowe zgłoszenie", fontWeight = FontWeight.SemiBold)
            }
        }
    }
}

@Composable private fun LicznikZgloszen(
    etykieta: String, wartosc: Int, barwa: Color, wybrany: Boolean,
    modifier: Modifier, onClick: () -> Unit,
) {
    val kolory = LocalCmdbColors.current
    Card(
        modifier.clickable(onClick = onClick),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(
            if (wybrany) 2.dp else 1.dp,
            if (wybrany) MaterialTheme.colorScheme.primary else kolory.cardBorder,
        ),
    ) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp)) {
            Text(etykieta, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
            Spacer(Modifier.height(3.dp))
            Text("$wartosc", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold, color = barwa)
        }
    }
}

@Composable private fun KartaZgloszenia(ticket: Ticket, wieleFirm: Boolean, onClick: () -> Unit) {
    val kolory = LocalCmdbColors.current
    Card(
        Modifier.fillMaxWidth().clickable(onClick = onClick),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, kolory.cardBorder),
    ) {
        Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    ticket.number,
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.weight(1f))
                StatusPill(ticket.statusLabel, kolorStatusu(ticket.status))
                Icon(Icons.Outlined.ChevronRight, null, Modifier.size(20.dp), MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Text(
                ticket.subject,
                style = MaterialTheme.typography.titleMedium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            val sprzet = ticket.assets.joinToString(", ") { it.hostname }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    if (sprzet.isBlank()) Icons.Outlined.Person else Icons.Outlined.Computer,
                    null, Modifier.size(15.dp), MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.width(6.dp))
                Text(
                    sprzet.ifBlank { ticket.requesterName ?: ticket.requesterEmail },
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (ticket.waiting) {
                    Box(Modifier.size(7.dp).clip(CircleShape).background(kolory.danger))
                    Spacer(Modifier.width(6.dp))
                    Text(
                        "Czeka na naszą odpowiedź · ${wzglednyCzas(ticket.waitingSince ?: ticket.lastActivity)}",
                        fontSize = 12.sp, color = kolory.danger, fontWeight = FontWeight.Medium,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                } else {
                    Text(
                        "Aktywność: ${wzglednyCzas(ticket.lastActivity)}",
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            Text(
                listOfNotNull(
                    if (wieleFirm) ticket.tenant.ifBlank { null } else null,
                    ticket.technician ?: "nieprzypisane",
                    ticket.typeLabel.ifBlank { null },
                ).joinToString(" • "),
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

// --- karta zgloszenia -------------------------------------------------------

@Composable internal fun HelpdeskTicketScreen(
    detail: TicketDetail,
    statusy: List<ReportOption>,
    assetPicker: List<TicketAsset>,
    padding: PaddingValues,
    saving: Boolean,
    mutationVersion: Int,
    onThread: () -> Unit,
    onStatus: (String, String) -> Unit,
    onAssign: (String) -> Unit,
    onTime: (Int, String) -> Unit,
    onAsset: (String, String) -> Unit,
    onSearchAssets: (String) -> Unit,
    onOpenAttachment: (TicketAttachment) -> Unit,
) {
    val kolory = LocalCmdbColors.current
    val ticket = detail.ticket
    var oknoStatusu by remember { mutableStateOf(false) }
    var oknoTechnika by remember { mutableStateOf(false) }
    var oknoCzasu by remember { mutableStateOf(false) }
    var oknoSprzetu by remember { mutableStateOf(false) }
    LaunchedEffect(mutationVersion) {
        oknoStatusu = false; oknoTechnika = false; oknoCzasu = false; oknoSprzetu = false
    }

    val opis = detail.entries.firstOrNull { it.kind == "od_klienta" }?.content.orEmpty()
    val zalaczniki = detail.entries.flatMap { it.attachments }
    val rozmowa = detail.entries.count { it.kind != "system" }

    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(0.dp),
    ) {
        item {
            Column(
                Modifier.fillMaxWidth().background(kolory.bar).padding(horizontal = 16.dp, vertical = 14.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Text(
                    ticket.subject,
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.Bold,
                    color = Color.White,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    StatusPill(ticket.statusLabel, kolorStatusu(ticket.status))
                    if (ticket.typeLabel.isNotBlank()) StatusPill(ticket.typeLabel, BrandCyan)
                }
                Text(
                    listOfNotNull(ticket.number, ticket.tenant.ifBlank { null }).joinToString(" • "),
                    style = MaterialTheme.typography.labelMedium,
                    color = Color.White.copy(alpha = .75f),
                )
            }
        }
        item {
            // Miejsce, w ktorym projekt pokazuje licznik SLA. System go nie ma,
            // wiec stoi tu jedyna prawdziwa pilnosc: od kiedy klient czeka.
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                PasekCzekania(detail)
                if (opis.isNotBlank()) {
                    ElevatedCmdbCard {
                        Text("Opis", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
                        Spacer(Modifier.height(6.dp))
                        Text(opis.trim(), maxLines = 12, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
        item {
            Column(Modifier.padding(horizontal = 14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                ElevatedCmdbCard(padding = 0.dp) {
                    WierszKarty(
                        Icons.Outlined.Computer, "Powiązane urządzenie",
                        ticket.assets.joinToString(", ") { it.hostname }.ifBlank { "brak" },
                        enabled = !saving,
                    ) { oknoSprzetu = true; onSearchAssets("") }
                    HorizontalDivider(color = kolory.cardBorder)
                    WierszKarty(
                        Icons.Outlined.Person, "Zgłaszający",
                        detail.requester.name ?: detail.requester.email, enabled = false,
                    ) {}
                    HorizontalDivider(color = kolory.cardBorder)
                    WierszKarty(
                        Icons.Outlined.SupervisorAccount, "Przypisany technik",
                        ticket.technician ?: "nieprzypisane", enabled = !saving,
                    ) { oknoTechnika = true }
                    HorizontalDivider(color = kolory.cardBorder)
                    WierszKarty(
                        Icons.Outlined.Timer, "Czas pracy",
                        detail.time.totalLabel, enabled = !saving,
                    ) { oknoCzasu = true }
                    HorizontalDivider(color = kolory.cardBorder)
                    WierszKarty(
                        Icons.Outlined.QuestionAnswer, "Rozmowa", "$rozmowa", enabled = true,
                        onClick = onThread,
                    )
                }
            }
        }
        if (detail.requester.fields.isNotEmpty()) item {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SectionHeading("Kartoteka zgłaszającego")
                ElevatedCmdbCard {
                    InfoLine("E-mail", detail.requester.email)
                    detail.requester.fields.forEach { pole -> InfoLine(pole.label, pole.value) }
                }
            }
        }
        if (zalaczniki.isNotEmpty()) item {
            Column(Modifier.padding(horizontal = 14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SectionHeading("Załączniki")
                ElevatedCmdbCard {
                    zalaczniki.forEach { plik ->
                        ZalacznikWiersz(plik) { onOpenAttachment(plik) }
                    }
                }
            }
        }
        if (detail.time.shares.isNotEmpty()) item {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SectionHeading("Czas pracy")
                ElevatedCmdbCard {
                    detail.time.shares.forEach { udzial -> InfoLine(udzial.technician, udzial.label) }
                }
            }
        }
        item {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = { oknoStatusu = true },
                    enabled = !saving,
                    modifier = Modifier.fillMaxWidth().height(50.dp),
                    shape = RoundedCornerShape(12.dp),
                ) { Text("Zmień status", fontWeight = FontWeight.SemiBold) }
                OutlinedButton(
                    onClick = onThread,
                    modifier = Modifier.fillMaxWidth().height(50.dp),
                    shape = RoundedCornerShape(12.dp),
                ) {
                    Icon(Icons.Outlined.QuestionAnswer, null)
                    Spacer(Modifier.width(8.dp))
                    Text("Odpisz w rozmowie")
                }
            }
        }
    }

    if (oknoStatusu) OknoStatusu(detail, statusy, saving, { oknoStatusu = false }, onStatus)
    if (oknoTechnika) OknoTechnika(detail, saving, { oknoTechnika = false }, onAssign)
    if (oknoCzasu) OknoCzasu(saving, { oknoCzasu = false }, onTime)
    if (oknoSprzetu) OknoSprzetu(detail, assetPicker, saving, { oknoSprzetu = false }, onSearchAssets, onAsset)
}

@Composable private fun PasekCzekania(detail: TicketDetail) {
    val kolory = LocalCmdbColors.current
    val ticket = detail.ticket
    val czeka = ticket.waiting
    val barwa = if (czeka) kolory.warn else MaterialTheme.colorScheme.onSurfaceVariant
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(barwa.copy(alpha = .14f))
            .border(1.dp, barwa.copy(alpha = .35f), RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(if (czeka) Icons.Outlined.HourglassEmpty else Icons.Outlined.Schedule, null, Modifier.size(20.dp), barwa)
        Spacer(Modifier.width(10.dp))
        Text(
            if (czeka) "Czeka na naszą odpowiedź od ${wzglednyCzas(ticket.waitingSince ?: ticket.lastActivity)}"
            else "Ostatnia aktywność: ${wzglednyCzas(ticket.lastActivity)}",
            color = barwa,
            fontWeight = FontWeight.Medium,
            style = MaterialTheme.typography.bodyMedium,
        )
    }
}

@Composable private fun WierszKarty(
    ikona: ImageVector,
    etykieta: String,
    wartosc: String,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().clickable(enabled = enabled, onClick = onClick).padding(horizontal = 15.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(ikona, null, Modifier.size(20.dp), MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(13.dp))
        Column(Modifier.weight(1f)) {
            Text(etykieta, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(wartosc, style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        if (enabled) Icon(Icons.Outlined.ChevronRight, null, Modifier.size(20.dp), MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun ZalacznikWiersz(plik: TicketAttachment, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Outlined.AttachFile, null, Modifier.size(18.dp), MaterialTheme.colorScheme.primary)
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(plik.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (plik.size > 0) Text(
                opisRozmiaru(plik.size),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Icon(Icons.Outlined.ChevronRight, null, Modifier.size(18.dp), MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun OknoStatusu(
    detail: TicketDetail, statusy: List<ReportOption>, saving: Boolean,
    onDismiss: () -> Unit, onStatus: (String, String) -> Unit,
) {
    var wybrany by remember { mutableStateOf(detail.ticket.status) }
    var podsumowanie by remember { mutableStateOf("") }
    val zamykamy = wybrany == "zamkniete" && detail.ticket.status != "zamkniete"
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Status zgłoszenia") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                statusy.forEach { status ->
                    CmdbFilterChip(status.label, wybrany == status.key) { wybrany = status.key }
                }
                if (zamykamy && detail.closingEmail) {
                    Text(
                        "Klient dostanie wiadomość o zakończeniu sprawy. Podsumowanie " +
                            "zostanie do niej dopisane.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    OutlinedTextField(
                        value = podsumowanie,
                        onValueChange = { podsumowanie = it },
                        label = { Text("Podsumowanie dla klienta") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 3,
                        shape = RoundedCornerShape(12.dp),
                    )
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onStatus(wybrany, podsumowanie) },
                enabled = !saving && wybrany != detail.ticket.status,
            ) { Text("Zapisz") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("Anuluj") } },
    )
}

@Composable private fun OknoTechnika(
    detail: TicketDetail, saving: Boolean, onDismiss: () -> Unit, onAssign: (String) -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Przypisz zgłoszenie") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                TextButton(onClick = { onAssign("") }, enabled = !saving) { Text("— bez technika —") }
                detail.technicians.forEach { technik ->
                    TextButton(
                        onClick = { onAssign(technik.id) },
                        enabled = !saving && technik.id != detail.ticket.technicianId,
                    ) { Text(technik.name) }
                }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("Zamknij") } },
    )
}

@Composable private fun OknoCzasu(saving: Boolean, onDismiss: () -> Unit, onTime: (Int, String) -> Unit) {
    var minuty by remember { mutableStateOf("") }
    var opis by remember { mutableStateOf("") }
    val liczba = minuty.trim().toIntOrNull()
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Dopisz czas pracy") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(
                    "Wpisu nie da się później zmienić ani skasować - pomyłkę prostuje " +
                        "kolejny wpis, np. „-15”, i wtedy opis jest wymagany.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedTextField(
                    value = minuty,
                    onValueChange = { minuty = it.filter { znak -> znak.isDigit() || znak == '-' } },
                    label = { Text("Minuty") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp),
                )
                OutlinedTextField(
                    value = opis,
                    onValueChange = { opis = it },
                    label = { Text("Opis") },
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp),
                )
            }
        },
        confirmButton = {
            Button(
                onClick = { liczba?.let { onTime(it, opis) } },
                enabled = !saving && liczba != null && liczba != 0 &&
                    (liczba > 0 || opis.isNotBlank()),
            ) { Text("Dopisz") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("Anuluj") } },
    )
}

@Composable private fun OknoSprzetu(
    detail: TicketDetail,
    assetPicker: List<TicketAsset>,
    saving: Boolean,
    onDismiss: () -> Unit,
    onSearchAssets: (String) -> Unit,
    onAsset: (String, String) -> Unit,
) {
    var szukaj by remember { mutableStateOf("") }
    LaunchedEffect(szukaj) {
        kotlinx.coroutines.delay(300)
        onSearchAssets(szukaj)
    }
    val podpiety = detail.ticket.assets.map { it.id }.toSet()
    // Sprzet zglaszajacego na gorze listy: to on jest szukany w wiekszosci
    // zgloszen, a wpisywanie nazwy hosta na telefonie jest karą.
    val propozycje = (detail.candidates + assetPicker).distinctBy { it.id }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Sprzęt zgłoszenia") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                detail.ticket.assets.forEach { asset ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(asset.hostname, Modifier.weight(1f), fontWeight = FontWeight.Medium)
                        TextButton(onClick = { onAsset(asset.id, "detach") }, enabled = !saving) { Text("Odepnij") }
                    }
                }
                OutlinedTextField(
                    value = szukaj,
                    onValueChange = { szukaj = it },
                    label = { Text("Szukaj sprzętu") },
                    leadingIcon = { Icon(Icons.Outlined.Search, null) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp),
                )
                propozycje.filter { it.id !in podpiety }.take(20).forEach { asset ->
                    Row(
                        Modifier.fillMaxWidth()
                            .clickable(enabled = !saving) { onAsset(asset.id, "attach") }
                            .padding(vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(Icons.Outlined.Computer, null, Modifier.size(18.dp), MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.width(10.dp))
                        val adres = asset.primaryIp
                        Column(Modifier.weight(1f)) {
                            Text(asset.hostname, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            if (!adres.isNullOrBlank()) Text(
                                adres,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Icon(Icons.Outlined.Add, null, Modifier.size(18.dp), MaterialTheme.colorScheme.primary)
                    }
                }
                if (propozycje.none { it.id !in podpiety }) Text(
                    "Brak sprzętu pasującego do wyszukiwania.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("Zamknij") } },
    )
}

// --- rozmowa ----------------------------------------------------------------

@Composable internal fun HelpdeskThreadScreen(
    detail: TicketDetail,
    padding: PaddingValues,
    saving: Boolean,
    mutationVersion: Int,
    onSend: (NewMessage, List<WybranyPlik>) -> Unit,
    onOpenAttachment: (TicketAttachment) -> Unit,
) {
    val kolory = LocalCmdbColors.current
    var tresc by remember { mutableStateOf("") }
    var doKlienta by remember { mutableStateOf(true) }
    var zostawWTrakcie by remember { mutableStateOf(false) }
    val pliki = remember { mutableStateListOf<WybranyPlik>() }
    val lista = rememberLazyListState()
    LaunchedEffect(mutationVersion) { tresc = ""; pliki.clear() }
    // Nad wpisami jest jeszcze naglowek watku, wiec ostatnia wiadomosc ma
    // indeks rowny ich liczbie.
    LaunchedEffect(detail.entries.size) {
        if (detail.entries.isNotEmpty()) lista.scrollToItem(detail.entries.size)
    }

    Column(Modifier.fillMaxSize().padding(padding).imePadding()) {
        LazyColumn(
            Modifier.weight(1f).fillMaxWidth(),
            state = lista,
            contentPadding = PaddingValues(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Text(
                    "${detail.ticket.number} · ${detail.ticket.subject}",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            items(detail.entries, key = { it.id }) { wpis ->
                if (wpis.kind == "system") WpisSystemowy(wpis)
                else DymekWiadomosci(wpis, onOpenAttachment)
            }
        }
        HorizontalDivider(color = kolory.cardBorder)
        Column(
            Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface).padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                CmdbFilterChip("Odpowiedź", doKlienta) { doKlienta = true }
                CmdbFilterChip("Notatka wewnętrzna", !doKlienta) { doKlienta = false }
            }
            if (doKlienta && !detail.mailbox) Text(
                "Skrzynka helpdesku jest wyłączona - odpowiedź zostanie zapisana, " +
                    "ale nie wyjdzie do klienta.",
                style = MaterialTheme.typography.bodySmall,
                color = kolory.warn,
            )
            if (!doKlienta) Text(
                "Notatka jest widoczna tylko dla techników.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            WybranePliki(pliki) { pliki.remove(it) }
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                PrzyciskiZalacznikow(enabled = !saving) { pliki.add(it) }
                OutlinedTextField(
                    value = tresc,
                    onValueChange = { tresc = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("Napisz wiadomość…", fontSize = 14.sp) },
                    maxLines = 5,
                    shape = RoundedCornerShape(18.dp),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedContainerColor = MaterialTheme.colorScheme.surface,
                        unfocusedContainerColor = MaterialTheme.colorScheme.surface,
                        focusedBorderColor = MaterialTheme.colorScheme.primary,
                        unfocusedBorderColor = kolory.cardBorder,
                    ),
                )
                IconButton(
                    onClick = {
                        onSend(
                            NewMessage(
                                content = tresc.trim(),
                                kind = if (doKlienta) "do_klienta" else "wewnetrzny",
                                keepInProgress = zostawWTrakcie,
                            ),
                            pliki.toList(),
                        )
                    },
                    enabled = !saving && tresc.isNotBlank(),
                    modifier = Modifier.size(48.dp).clip(CircleShape).background(
                        if (!saving && tresc.isNotBlank()) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.surfaceVariant,
                    ),
                ) { Icon(Icons.AutoMirrored.Outlined.Send, "Wyślij", tint = Color.White) }
            }
            if (doKlienta) Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(checked = zostawWTrakcie, onCheckedChange = { zostawWTrakcie = it }, enabled = !saving)
                Text(
                    "Zostaw w toku (nie czekam na odpowiedź klienta)",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable private fun WpisSystemowy(wpis: TicketEntry) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
        Text(
            "${wpis.content} · ${wzglednyCzas(wpis.createdAt)}",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable private fun DymekWiadomosci(wpis: TicketEntry, onOpenAttachment: (TicketAttachment) -> Unit) {
    val kolory = LocalCmdbColors.current
    val odKlienta = wpis.kind == "od_klienta"
    val wewnetrzny = wpis.kind == "wewnetrzny"
    val tlo = when {
        wewnetrzny -> kolory.warn.copy(alpha = .16f)
        odKlienta -> MaterialTheme.colorScheme.surfaceVariant
        else -> MaterialTheme.colorScheme.primaryContainer
    }
    Column(
        Modifier.fillMaxWidth(),
        horizontalAlignment = if (odKlienta) Alignment.Start else Alignment.End,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (wewnetrzny) {
                Icon(Icons.Outlined.Lock, null, Modifier.size(13.dp), kolory.warn)
                Spacer(Modifier.width(4.dp))
            }
            Text(
                "${wpis.author} · ${wzglednyCzas(wpis.createdAt)}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(4.dp))
        Column(
            Modifier.fillMaxWidth(.92f)
                .clip(RoundedCornerShape(14.dp))
                .background(tlo)
                .padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (wewnetrzny) Text(
                "Notatka wewnętrzna",
                style = MaterialTheme.typography.labelSmall,
                color = kolory.warn,
                fontWeight = FontWeight.Bold,
            )
            if (wpis.content.isNotBlank()) Text(wpis.content.trim())
            wpis.attachments.forEach { plik ->
                Row(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .clickable { onOpenAttachment(plik) }
                        .padding(10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(Icons.Outlined.AttachFile, null, Modifier.size(18.dp), MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(8.dp))
                    Column(Modifier.weight(1f)) {
                        Text(plik.name, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        if (plik.size > 0) Text(
                            opisRozmiaru(plik.size),
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
            // Odpowiedz, ktora nie wyszla, wyglada w watku tak samo jak wyslana -
            // i to jest najgorszy moment, zeby technik sie pomylil.
            val blad = wpis.error
            if (!blad.isNullOrBlank()) Text(
                "Nie wyszło: $blad",
                style = MaterialTheme.typography.labelSmall,
                color = kolory.danger,
                fontWeight = FontWeight.Bold,
            )
            else if (wpis.kind == "do_klienta" && wpis.sentAt != null) Text(
                "Wysłane do klienta",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

// --- zalaczniki w formularzach ---------------------------------------------

@Composable private fun PrzyciskiZalacznikow(enabled: Boolean, onPick: (WybranyPlik) -> Unit) {
    val context = LocalContext.current
    var adresZdjecia by remember { mutableStateOf<Uri?>(null) }
    val zPliku = rememberLauncherForActivityResult(ActivityResultContracts.GetMultipleContents()) { adresy ->
        adresy.forEach { adres -> onPick(context.opisPliku(adres)) }
    }
    val zAparatu = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { udane ->
        val adres = adresZdjecia
        if (udane && adres != null) onPick(context.opisPliku(adres))
    }
    IconButton(onClick = { zPliku.launch("*/*") }, enabled = enabled) {
        Icon(Icons.Outlined.AttachFile, "Dodaj plik", tint = MaterialTheme.colorScheme.onSurfaceVariant)
    }
    IconButton(
        onClick = {
            val (adres, _) = context.adresNaZdjecie()
            adresZdjecia = adres
            zAparatu.launch(adres)
        },
        enabled = enabled,
    ) { Icon(Icons.Outlined.PhotoCamera, "Zrób zdjęcie", tint = MaterialTheme.colorScheme.onSurfaceVariant) }
}

@Composable private fun WybranePliki(pliki: List<WybranyPlik>, onRemove: (WybranyPlik) -> Unit) {
    if (pliki.isEmpty()) return
    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        items(pliki, key = { it.uri.toString() }) { plik ->
            Row(
                Modifier.clip(RoundedCornerShape(16.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(start = 10.dp, end = 4.dp, top = 4.dp, bottom = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    plik.nazwa + (if (plik.rozmiar > 0) " · ${opisRozmiaru(plik.rozmiar)}" else ""),
                    fontSize = 12.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.widthIn(max = 180.dp),
                )
                IconButton(onClick = { onRemove(plik) }, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Outlined.Close, "Usuń plik", Modifier.size(16.dp))
                }
            }
        }
    }
}

// --- nowe zgloszenie --------------------------------------------------------

@Composable internal fun HelpdeskNewTicketScreen(
    catalog: HelpdeskCatalog?,
    assetPicker: List<TicketAsset>,
    padding: PaddingValues,
    saving: Boolean,
    onSearchAssets: (String, String) -> Unit,
    onCreate: (NewTicket, List<WybranyPlik>) -> Unit,
) {
    val firmy = catalog?.tenants.orEmpty()
    // Firma ustawia sie sama tylko wtedy, gdy konto obsluguje dokladnie jedna.
    // Odswiezenie katalogu w tle nie moze skasowac wyboru technika, ktory
    // wlasnie wypelnia formularz - stad warunek na pusta wartosc.
    var firma by remember { mutableStateOf("") }
    LaunchedEffect(firmy) { if (firma.isBlank()) firma = firmy.singleOrNull()?.id.orEmpty() }
    var email by remember { mutableStateOf("") }
    var nazwa by remember { mutableStateOf("") }
    var temat by remember { mutableStateOf("") }
    var opis by remember { mutableStateOf("") }
    var rodzaj by remember { mutableStateOf("") }
    var zrodlo by remember { mutableStateOf("telefon") }
    var sprzet by remember { mutableStateOf<TicketAsset?>(null) }
    var szukajSprzetu by remember { mutableStateOf("") }
    var doMnie by remember { mutableStateOf(true) }
    var powiadom by remember { mutableStateOf(true) }
    val pliki = remember { mutableStateListOf<WybranyPlik>() }

    LaunchedEffect(firma, szukajSprzetu) {
        if (firma.isNotBlank()) {
            kotlinx.coroutines.delay(300)
            onSearchAssets(firma, szukajSprzetu)
        }
    }

    val poprawne = firma.isNotBlank() && email.contains("@") && temat.isNotBlank() && opis.isNotBlank()

    LazyColumn(
        Modifier.fillMaxSize().padding(padding).imePadding(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        if (firmy.size > 1) item {
            PoleFormularza("Firma", wymagane = true) {
                ModernChoiceField("Firma", firma, firmy.map { it.id to it.name }) { firma = it }
            }
        }
        item {
            PoleFormularza("Zgłaszający (e-mail)", wymagane = true) {
                OutlinedTextField(
                    value = email,
                    onValueChange = { email = it },
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("jan.kowalski@firma.pl") },
                    leadingIcon = { Icon(Icons.Outlined.MailOutline, null) },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                    shape = RoundedCornerShape(12.dp),
                )
            }
        }
        item {
            PoleFormularza("Imię i nazwisko") {
                OutlinedTextField(
                    value = nazwa,
                    onValueChange = { nazwa = it },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Outlined.Person, null) },
                    singleLine = true,
                    shape = RoundedCornerShape(12.dp),
                )
            }
        }
        item {
            PoleFormularza("Temat", wymagane = true) {
                OutlinedTextField(
                    value = temat,
                    onValueChange = { temat = it },
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("Brak dostępu do VPN") },
                    leadingIcon = { Icon(Icons.Outlined.ConfirmationNumber, null) },
                    singleLine = true,
                    shape = RoundedCornerShape(12.dp),
                )
            }
        }
        item {
            // W projekcie jest tu "Kategoria". System zna rodzaj sprawy
            // (incydent / prośba / inne) i to jego pokazujemy - kategorii
            // zgloszen CMDB nie ma.
            PoleFormularza("Rodzaj sprawy") {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    item { CmdbFilterChip("Nie określam", rodzaj.isBlank()) { rodzaj = "" } }
                    items(catalog?.types.orEmpty(), key = { it.key }) { typ ->
                        CmdbFilterChip(typ.label, rodzaj == typ.key) { rodzaj = typ.key }
                    }
                }
            }
        }
        item {
            PoleFormularza("Skąd zgłoszenie") {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(catalog?.sources.orEmpty(), key = { it.key }) { pozycja ->
                        CmdbFilterChip(pozycja.label, zrodlo == pozycja.key) { zrodlo = pozycja.key }
                    }
                }
            }
        }
        item {
            PoleFormularza("Urządzenie") {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        value = sprzet?.hostname ?: szukajSprzetu,
                        onValueChange = { szukajSprzetu = it; sprzet = null },
                        modifier = Modifier.fillMaxWidth(),
                        enabled = firma.isNotBlank(),
                        placeholder = { Text("Szukaj po nazwie, IP lub numerze") },
                        leadingIcon = { Icon(Icons.Outlined.Computer, null) },
                        trailingIcon = {
                            if (sprzet != null) IconButton(onClick = { sprzet = null; szukajSprzetu = "" }) {
                                Icon(Icons.Outlined.Close, "Wyczyść")
                            }
                        },
                        singleLine = true,
                        shape = RoundedCornerShape(12.dp),
                    )
                    if (sprzet == null && szukajSprzetu.isNotBlank()) {
                        assetPicker.take(6).forEach { asset ->
                            Row(
                                Modifier.fillMaxWidth().clickable { sprzet = asset }.padding(vertical = 8.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Icon(Icons.Outlined.Computer, null, Modifier.size(18.dp), MaterialTheme.colorScheme.onSurfaceVariant)
                                Spacer(Modifier.width(10.dp))
                                val adres = asset.primaryIp
                                Text(asset.hostname, Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                                if (!adres.isNullOrBlank()) Text(
                                    adres,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                }
            }
        }
        item {
            PoleFormularza("Opis", wymagane = true) {
                OutlinedTextField(
                    value = opis,
                    onValueChange = { opis = it },
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("Opisz problem…") },
                    leadingIcon = { Icon(Icons.Outlined.Description, null) },
                    minLines = 4,
                    shape = RoundedCornerShape(12.dp),
                )
            }
        }
        item {
            PoleFormularza("Załączniki") {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        PrzyciskiZalacznikow(enabled = !saving) { pliki.add(it) }
                        Text(
                            "Zdjęcie lub plik - do ${catalog?.attachmentMb ?: 0} MB",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    WybranePliki(pliki) { pliki.remove(it) }
                }
            }
        }
        item {
            Column {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = doMnie, onCheckedChange = { doMnie = it }, enabled = !saving)
                    Text("Przypisz do mnie i ustaw „W trakcie”")
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(
                        checked = powiadom && catalog?.mailbox == true,
                        onCheckedChange = { powiadom = it },
                        enabled = !saving && catalog?.mailbox == true,
                    )
                    Text(
                        if (catalog?.mailbox == true) "Wyślij klientowi numer zgłoszenia"
                        else "Skrzynka wyłączona - klient nie dostanie numeru",
                        color = if (catalog?.mailbox == true) MaterialTheme.colorScheme.onSurface
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
        item {
            Text(
                "Pola z * są wymagane.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        item {
            Button(
                onClick = {
                    onCreate(
                        NewTicket(
                            tenantId = firma,
                            requesterEmail = email.trim(),
                            requesterName = nazwa.trim(),
                            subject = temat.trim(),
                            content = opis.trim(),
                            type = rodzaj,
                            source = zrodlo,
                            assetId = sprzet?.id.orEmpty(),
                            assignToMe = doMnie,
                            notifyCustomer = powiadom && catalog?.mailbox == true,
                        ),
                        pliki.toList(),
                    )
                },
                enabled = !saving && poprawne,
                modifier = Modifier.fillMaxWidth().height(50.dp),
                shape = RoundedCornerShape(12.dp),
            ) { Text("Wyślij zgłoszenie", fontWeight = FontWeight.SemiBold) }
        }
    }
}

@Composable private fun PoleFormularza(
    etykieta: String,
    wymagane: Boolean = false,
    content: @Composable () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(
            etykieta + if (wymagane) " *" else "",
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        content()
    }
}
