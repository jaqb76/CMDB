package pl.hubzso.cmdb.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.AccountTree
import androidx.compose.material.icons.outlined.Apps
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.DeveloperBoard
import androidx.compose.material.icons.outlined.ErrorOutline
import androidx.compose.material.icons.outlined.Group
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.Info
import androidx.compose.material.icons.outlined.KeyboardArrowDown
import androidx.compose.material.icons.outlined.Lan
import androidx.compose.material.icons.outlined.Memory
import androidx.compose.material.icons.outlined.Person
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material.icons.outlined.Security
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.Storage
import androidx.compose.material.icons.outlined.SystemUpdate
import androidx.compose.material.icons.outlined.Wifi
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.longOrNull
import pl.hubzso.cmdb.data.Vulnerabilities
import pl.hubzso.cmdb.data.Vulnerability
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/*
 * Czytelny widok danych zasobu. Raport agenta to zagniezdzony JSON - zamiast
 * pokazywac go surowo, kazda sekcja to zwijana karta, listy obiektow to osobne
 * wiersze z tytulem, a bajty, procenty i daty sa formatowane po ludzku.
 */

// ---------------------------------------------------------------------------
// Zwijana karta
// ---------------------------------------------------------------------------

@Composable internal fun ExpandableCard(
    title: String,
    summary: String?,
    icon: ImageVector,
    key: String,
    expandable: Boolean = true,
    content: @Composable () -> Unit,
) {
    var open by rememberSaveable(key) { mutableStateOf(false) }
    val rotation by animateFloatAsState(if (open) 180f else 0f, label = "strzalka")
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, LocalCmdbColors.current.cardBorder),
    ) {
        Row(
            Modifier.fillMaxWidth()
                .then(if (expandable) Modifier.clickable { open = !open } else Modifier)
                .padding(horizontal = 15.dp, vertical = 13.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(icon, null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(22.dp))
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
                if (!summary.isNullOrBlank()) {
                    Text(summary, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Medium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                }
            }
            if (expandable) Icon(Icons.Outlined.KeyboardArrowDown, if (open) "Zwiń" else "Rozwiń", Modifier.rotate(rotation), tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (expandable) AnimatedVisibility(open) {
            Column(Modifier.fillMaxWidth()) {
                HorizontalDivider(color = LocalCmdbColors.current.cardBorder)
                Column(Modifier.fillMaxWidth().padding(15.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) { content() }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Zakladka "Sprzet": podsumowanie z faktow + szczegoly z raportu
// ---------------------------------------------------------------------------

private data class FactSpec(val label: String, val icon: ImageVector, val detailPath: List<String>? = null, val unit: String? = null)

private val factSpecs: Map<String, FactSpec> = linkedMapOf(
    "cpu_model" to FactSpec("Procesor", Icons.Outlined.DeveloperBoard, listOf("hardware", "cpu")),
    "cpu_cores" to FactSpec("Rdzenie fizyczne", Icons.Outlined.DeveloperBoard),
    "cpu_threads" to FactSpec("Wątki (rdzenie logiczne)", Icons.Outlined.DeveloperBoard),
    "memory_gb" to FactSpec("Pamięć RAM", Icons.Outlined.Memory, listOf("hardware", "memory"), "GB"),
    "storage_gb" to FactSpec("Pojemność dysków", Icons.Outlined.Storage, listOf("hardware", "storage"), "GB"),
    "disks" to FactSpec("Dyski", Icons.Outlined.Storage, listOf("hardware", "storage")),
    "updates" to FactSpec("Zainstalowane aktualizacje", Icons.Outlined.SystemUpdate, listOf("software", "updates")),
    "packages" to FactSpec("Oprogramowanie", Icons.Outlined.Apps, listOf("software", "packages")),
    "services_total" to FactSpec("Usługi", Icons.Outlined.Settings, listOf("software", "services")),
    "services_running" to FactSpec("Usługi uruchomione", Icons.Outlined.Settings),
    "processes" to FactSpec("Procesy", Icons.Outlined.AccountTree, listOf("software", "processes")),
    "local_users" to FactSpec("Konta lokalne", Icons.Outlined.Person, listOf("users", "local_accounts")),
    "administrators" to FactSpec("Administratorzy", Icons.Outlined.Security, listOf("users", "administrators")),
    "sessions" to FactSpec("Sesje użytkowników", Icons.Outlined.Group, listOf("users", "sessions")),
    "ip_addresses" to FactSpec("Adresy IP", Icons.Outlined.Lan),
    "mac_addresses" to FactSpec("Adresy MAC", Icons.Outlined.Lan),
    "last_boot" to FactSpec("Ostatnie uruchomienie", Icons.Outlined.Schedule),
    "collector_errors" to FactSpec("Błędy zbierania", Icons.Outlined.ErrorOutline, listOf("errors")),
)

@Composable internal fun HardwareTab(facts: Map<String, JsonElement>, report: JsonObject?) {
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading("Sprzęt")
        if (facts.isEmpty()) EmptyState("Brak danych w tej sekcji")
        // Znane klucze w ustalonej kolejnosci, nieznane na koncu. Pola
        // dopisane do karty innego pola nie dostaja wlasnej karty.
        val folded = foldedFacts.filterValues { it in facts }.keys
        val keys = (factSpecs.keys.filter { it in facts } + facts.keys.filter { it !in factSpecs }) - folded
        keys.forEach { key ->
            val value = facts.getValue(key)
            val spec = factSpecs[key] ?: FactSpec(labelFor(key), Icons.Outlined.Info)
            val detail = spec.detailPath?.let { report.at(it) }?.takeUnless { it.isEmpty() }
            val listValue = value as? JsonArray
            val summary = when {
                listValue != null -> if (listValue.isEmpty()) "brak" else listValue.size.toString()
                spec.unit != null -> "${formatPrimitive(key, value)} ${spec.unit}"
                else -> formatPrimitive(key, value)
            } + foldedSummary(key, facts)
            val expandable = detail != null || (listValue != null && listValue.isNotEmpty())
            ExpandableCard(spec.label, summary, spec.icon, "fakt-$key", expandable) {
                when {
                    key == "disks" || key == "storage_gb" -> StorageDetails(detail as? JsonObject)
                    detail != null -> JsonBody(detail, key)
                    listValue != null -> JsonBody(listValue, key)
                }
            }
        }
    }
}

/**
 * Pola, ktore dopisujemy do karty innego pola zamiast pokazywac osobno:
 * "Pojemność dysków" to ta sama rzecz co "Dyski", rdzenie to czesc
 * "Procesora". Wartosc to pole, do ktorego karty trafiaja.
 */
private val foldedFacts = mapOf(
    "storage_gb" to "disks",
    "cpu_cores" to "cpu_model",
    "cpu_threads" to "cpu_model",
    "services_running" to "services_total",
)

private fun foldedSummary(key: String, facts: Map<String, JsonElement>): String {
    fun int(k: String) = (facts[k] as? JsonPrimitive)?.let { it.longOrNull ?: it.doubleOrNull?.toLong() }?.toInt()
    val extra = when (key) {
        "disks" -> facts["storage_gb"]?.takeUnless { it is JsonNull }?.let { "razem ${formatPrimitive("storage_gb", it)} GB" }
        "cpu_model" -> listOfNotNull(
            int("cpu_cores")?.let { "$it ${odmiana(it, "rdzeń", "rdzenie", "rdzeni")}" },
            int("cpu_threads")?.let { "$it ${odmiana(it, "wątek", "wątki", "wątków")}" },
        ).joinToString(", ").ifEmpty { null }
        "services_total" -> int("services_running")?.let { "$it ${odmiana(it, "uruchomiona", "uruchomione", "uruchomionych")}" }
        else -> null
    }
    return extra?.let { " · $it" }.orEmpty()
}

@Composable private fun StorageDetails(storage: JsonObject?) {
    val logical = storage?.get("logical_disks") as? JsonArray
    val physical = storage?.get("physical_disks") as? JsonArray
    if (logical.isNullOrEmpty() && physical.isNullOrEmpty()) {
        Text("Agent nie przesłał szczegółów dysków", color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    if (!logical.isNullOrEmpty()) {
        SubHeading("Woluminy")
        logical.forEach { VolumeRow(it as? JsonObject ?: return@forEach) }
    }
    if (!physical.isNullOrEmpty()) {
        SubHeading("Dyski fizyczne")
        physical.forEach { ObjectItem(it as? JsonObject ?: return@forEach, "physical_disks") }
    }
}

@Composable private fun VolumeRow(disk: JsonObject) {
    val kolory = LocalCmdbColors.current
    val size = disk.long("size_bytes")
    val free = disk.long("free_bytes")
    val used = disk.double("used_percent") ?: if (size != null && free != null && size > 0) (size - free) * 100.0 / size else null
    val barColor = when {
        used == null -> MaterialTheme.colorScheme.primary
        used >= 90 -> kolory.danger
        used >= 75 -> kolory.warn
        else -> kolory.ok
    }
    ItemBox {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val name = listOfNotNull(disk.str("mount"), disk.str("label")?.let { "($it)" }).joinToString(" ").ifBlank { "Wolumin" }
            Text(name, Modifier.weight(1f), fontWeight = FontWeight.SemiBold)
            disk.str("filesystem")?.let { Text(it, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        if (used != null) {
            Spacer(Modifier.height(8.dp))
            LinearProgressIndicator(
                progress = { (used / 100.0).toFloat().coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().height(8.dp).clip(RoundedCornerShape(4.dp)),
                color = barColor,
                trackColor = barColor.copy(alpha = .18f),
                strokeCap = StrokeCap.Round,
                drawStopIndicator = {},
            )
        }
        Spacer(Modifier.height(6.dp))
        val opis = buildList {
            if (free != null) add("wolne ${formatBytes(free)}")
            if (size != null) add("z ${formatBytes(size)}")
            if (used != null) add("zajęte ${formatNumber(used)}%")
        }.joinToString(" · ")
        if (opis.isNotEmpty()) Text(opis, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// ---------------------------------------------------------------------------
// Zakladki "System" i "Siec": raport agenta w zwijanych sekcjach
// ---------------------------------------------------------------------------

private val sectionIcons = mapOf(
    "hardware" to Icons.Outlined.Memory,
    "os" to Icons.Outlined.Computer,
    "system" to Icons.Outlined.Computer,
    "network" to Icons.Outlined.Wifi,
    "software" to Icons.Outlined.Apps,
    "users" to Icons.Outlined.Group,
    "errors" to Icons.Outlined.ErrorOutline,
)

@Composable internal fun ReportTab(title: String, report: JsonObject?, skip: Set<String> = emptySet()) {
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading(title)
        val entries = report?.filterKeys { it !in skip }.orEmpty().filterValues { !it.isEmpty() }
        if (entries.isEmpty()) {
            EmptyState("Brak danych w tej sekcji")
            return@Column
        }
        // Proste pola (kolektor, czas zbierania...) w jednej karcie na gorze.
        val simple = entries.filterValues { it is JsonPrimitive }
        if (simple.isNotEmpty()) {
            ElevatedCmdbCard { simple.forEach { (k, v) -> KeyValueRow(labelFor(k), formatPrimitive(k, v)) } }
        }
        entries.filterValues { it !is JsonPrimitive }.forEach { (key, value) ->
            ExpandableCard(labelFor(key), summaryOf(value), sectionIcons[key] ?: Icons.Outlined.Info, "sekcja-$title-$key") {
                JsonBody(value, key)
            }
        }
    }
}

@Composable internal fun NetworkTab(report: JsonObject?, attributes: Map<String, JsonElement>) {
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading("Sieć")
        val network = report?.get("network") as? JsonObject
        val interfaces = network?.get("interfaces") as? JsonArray
        if (interfaces.isNullOrEmpty() && attributes.isEmpty()) EmptyState("Brak danych w tej sekcji")
        interfaces?.forEachIndexed { index, element ->
            val iface = element as? JsonObject ?: return@forEachIndexed
            val up = (iface["is_up"] as? JsonPrimitive)?.booleanOrNull
            val ips = (iface["ip_addresses"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }.orEmpty()
            val summary = ips.firstOrNull { !it.contains(':') } ?: ips.firstOrNull() ?: if (up == false) "rozłączony" else null
            val icon = if (iface.str("name")?.contains("wi", true) == true) Icons.Outlined.Wifi else Icons.Outlined.Lan
            ExpandableCard(iface.str("name") ?: "Interfejs ${index + 1}", summary, icon, "iface-$index") {
                JsonBody(iface, "interfaces")
            }
        }
        network?.filterKeys { it != "interfaces" }?.filterValues { !it.isEmpty() }?.forEach { (key, value) ->
            ExpandableCard(labelFor(key), summaryOf(value), Icons.Outlined.Lan, "siec-$key") { JsonBody(value, key) }
        }
        if (attributes.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            SectionHeading("Atrybuty")
            ElevatedCmdbCard {
                attributes.forEach { (k, v) ->
                    if (v is JsonPrimitive || v is JsonNull) KeyValueRow(labelFor(k), formatPrimitive(k, v))
                    else { SubHeading(labelFor(k)); JsonBody(v, k) }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Ogolny, czytelny renderer JSON-a
// ---------------------------------------------------------------------------

private const val PAGE = 15

@Composable private fun JsonBody(value: JsonElement, key: String) {
    when (value) {
        is JsonObject -> ObjectFields(value)
        is JsonArray -> ArrayItems(value, key)
        else -> Text(formatPrimitive(key, value))
    }
}

@Composable private fun ObjectFields(obj: JsonObject) {
    val entries = obj.filterValues { !it.isEmpty() }
    if (entries.isEmpty()) { Text("brak danych", color = MaterialTheme.colorScheme.onSurfaceVariant); return }
    entries.forEach { (k, v) ->
        when {
            v is JsonPrimitive -> KeyValueRow(labelFor(k), formatPrimitive(k, v))
            v is JsonArray && v.all { it is JsonPrimitive } -> KeyValueRow(labelFor(k), v.joinToString("\n") { formatPrimitive(k, it) })
            else -> {
                SubHeading(labelFor(k) + (summaryOf(v)?.let { " · $it" } ?: ""))
                JsonBody(v, k)
            }
        }
    }
}

@Composable private fun ArrayItems(array: JsonArray, key: String) {
    if (array.isEmpty()) { Text("brak", color = MaterialTheme.colorScheme.onSurfaceVariant); return }
    var limit by rememberSaveable(key, array.size) { mutableStateOf(PAGE) }
    array.take(limit).forEach { item ->
        when (item) {
            is JsonObject -> ObjectItem(item, key)
            is JsonArray -> ItemBox { JsonBody(item, key) }
            else -> Text("• " + formatPrimitive(key, item))
        }
    }
    if (array.size > limit) {
        TextButton(onClick = { limit += PAGE * 2 }) { Text("Pokaż więcej (${array.size - limit} pozostało)") }
    }
}

/** Element listy: tytul z najbardziej opisowego pola, reszta jako wiersze. */
@Composable private fun ObjectItem(obj: JsonObject, parentKey: String) {
    val titleKey = titleKeys.firstOrNull { obj.str(it) != null }
    val title = titleKey?.let { obj.str(it)?.let { t -> if (it.endsWith("_bytes")) formatBytes(t.toLongOrNull()) else t } }
    val subtitleKey = subtitleKeys[parentKey]?.firstOrNull { it != titleKey && obj.str(it) != null }
    ItemBox {
        if (title != null) {
            Text(title, fontWeight = FontWeight.SemiBold)
            subtitleKey?.let { Text(formatPrimitive(it, obj.getValue(it)), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        val rest = obj.filterKeys { it != titleKey && it != subtitleKey }.filterValues { !it.isEmpty() }
        if (rest.isNotEmpty()) {
            if (title != null) Spacer(Modifier.height(4.dp))
            ObjectFields(JsonObject(rest))
        }
    }
}

private val titleKeys = listOf("display_name", "name", "title", "model", "mount", "user", "id", "slot", "full_name")
private val subtitleKeys = mapOf(
    "updates" to listOf("description"),
    "packages" to listOf("version"),
    "services" to listOf("name"),
    "sessions" to listOf("session_type"),
    "administrators" to listOf("type"),
)

@Composable private fun ItemBox(content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.onSurface.copy(alpha = .05f))
            .padding(horizontal = 12.dp, vertical = 10.dp),
    ) { content() }
}

@Composable private fun SubHeading(text: String) {
    Text(text, Modifier.padding(top = 4.dp), style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
}

@Composable private fun KeyValueRow(label: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
        Text(label, Modifier.weight(1f).padding(end = 10.dp), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, Modifier.weight(1.3f), style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
    }
}

// ---------------------------------------------------------------------------
// Formatowanie
// ---------------------------------------------------------------------------

private val labels = mapOf(
    // sekcje
    "hardware" to "Sprzęt", "software" to "Oprogramowanie", "network" to "Sieć", "users" to "Użytkownicy",
    "os" to "System operacyjny", "system" to "Komputer", "bios" to "BIOS", "errors" to "Błędy zbierania",
    "cpu" to "Procesor", "memory" to "Pamięć", "storage" to "Dyski", "gpus" to "Karty graficzne", "load" to "Obciążenie",
    "physical_disks" to "Dyski fizyczne", "logical_disks" to "Woluminy", "modules" to "Moduły pamięci",
    "interfaces" to "Interfejsy", "packages" to "Oprogramowanie", "updates" to "Aktualizacje",
    "pending_updates" to "Oczekujące aktualizacje", "services" to "Usługi", "processes" to "Procesy",
    "local_accounts" to "Konta lokalne", "groups" to "Grupy", "administrators" to "Administratorzy", "sessions" to "Sesje",
    // pola
    "hostname" to "Nazwa hosta", "fqdn" to "FQDN", "domain" to "Domena", "os_family" to "Rodzina systemu",
    "arch" to "Architektura", "architecture" to "Architektura", "manufacturer" to "Producent", "model" to "Model",
    "serial_number" to "Numer seryjny", "uuid" to "UUID", "machine_guid" to "Machine GUID", "chassis" to "Obudowa",
    "system_type" to "Typ systemu", "part_of_domain" to "W domenie", "virtualization" to "Wirtualizacja",
    "vendor" to "Dostawca", "version" to "Wersja", "release_date" to "Data wydania", "name" to "Nazwa",
    "build" to "Kompilacja", "edition" to "Edycja", "install_date" to "Data instalacji", "last_boot" to "Ostatnie uruchomienie",
    "locale" to "Język", "timezone" to "Strefa czasowa", "free_physical_memory_bytes" to "Wolna pamięć",
    "physical_cores" to "Rdzenie fizyczne", "logical_cores" to "Rdzenie logiczne", "max_clock_mhz" to "Taktowanie",
    "sockets" to "Gniazda", "slot" to "Gniazdo", "capacity_bytes" to "Pojemność", "speed_mhz" to "Szybkość",
    "type" to "Typ", "part_number" to "Numer części", "total_bytes" to "Razem", "available_bytes" to "Dostępne",
    "percent" to "Użycie", "samples" to "Próbki", "source" to "Źródło", "size_bytes" to "Rozmiar",
    "interface" to "Interfejs", "status" to "Stan", "partitions" to "Partycje", "media_type" to "Rodzaj nośnika",
    "mount" to "Punkt montowania", "label" to "Etykieta", "filesystem" to "System plików", "free_bytes" to "Wolne",
    "used_percent" to "Zajęte", "driver_version" to "Wersja sterownika", "memory_bytes" to "Pamięć",
    "description" to "Opis", "mac_address" to "Adres MAC", "ip_addresses" to "Adresy IP", "gateways" to "Bramy",
    "dns_servers" to "Serwery DNS", "dhcp_enabled" to "DHCP", "dhcp_server" to "Serwer DHCP", "is_up" to "Aktywny",
    "publisher" to "Wydawca", "install_location" to "Lokalizacja", "scope" to "Zakres", "display_name" to "Nazwa wyświetlana",
    "state" to "Stan", "start_mode" to "Uruchamianie", "account" to "Konto", "path" to "Ścieżka",
    "id" to "Identyfikator", "installed_on" to "Zainstalowano", "title" to "Tytuł", "current_version" to "Obecna wersja",
    "new_version" to "Nowa wersja", "source_repo" to "Repozytorium", "security" to "Bezpieczeństwo", "severity" to "Ważność",
    "pid" to "PID", "full_name" to "Imię i nazwisko", "sid" to "SID", "enabled" to "Włączone", "locked" to "Zablokowane",
    "password_expires" to "Hasło wygasa", "password_required" to "Hasło wymagane", "last_logon" to "Ostatnie logowanie",
    "user" to "Użytkownik", "session_type" to "Rodzaj sesji", "members" to "Członkowie",
    "collector" to "Kolektor", "duration_ms" to "Czas zbierania", "collected_at" to "Zebrano",
    "agent_version" to "Wersja agenta", "schema_version" to "Wersja schematu",
)

internal fun labelFor(key: String): String = labels[key] ?: key.replace('_', ' ').replaceFirstChar { it.uppercase() }

private val dateFormat = DateTimeFormatter.ofPattern("d.MM.yyyy HH:mm", Locale.forLanguageTag("pl"))

internal fun formatPrimitive(key: String, value: JsonElement): String {
    if (value is JsonNull || value !is JsonPrimitive) return if (value is JsonNull) "—" else value.toString()
    value.booleanOrNull?.takeIf { !value.isString }?.let { return if (it) "Tak" else "Nie" }
    val text = value.contentOrNull ?: return "—"
    val number = if (value.isString) null else value.doubleOrNull
    return when {
        key.endsWith("_bytes") -> formatBytes(value.longOrNull ?: text.toLongOrNull())
        number != null && (key.endsWith("_percent") || key == "percent") -> "${formatNumber(number)}%"
        number != null && key.endsWith("_mhz") -> if (number >= 1000) "${formatNumber(number / 1000)} GHz" else "${formatNumber(number)} MHz"
        number != null && key.endsWith("_ms") -> if (number >= 1000) "${formatNumber(number / 1000)} s" else "${formatNumber(number)} ms"
        number != null -> formatNumber(number)
        else -> formatDate(text) ?: text.replace(Regex(" {2,}"), " ")
    }
}

private fun formatDate(text: String): String? {
    if (text.length < 10 || !text[4].equals('-') || !text.contains('T')) return null
    return chwila(text)?.atZone(ZoneId.systemDefault())?.format(dateFormat)
}

internal fun formatBytes(bytes: Long?): String {
    if (bytes == null) return "—"
    val units = listOf("B", "KB", "MB", "GB", "TB", "PB")
    var value = bytes.toDouble()
    var unit = 0
    while (value >= 1024 && unit < units.lastIndex) { value /= 1024; unit++ }
    return "${formatNumber(value)} ${units[unit]}"
}

private fun formatNumber(value: Double): String =
    if (value == Math.floor(value) && kotlin.math.abs(value) < 1e15) value.toLong().toString()
    else String.format(Locale.forLanguageTag("pl"), "%.1f", value)

private fun summaryOf(value: JsonElement): String? = when (value) {
    is JsonArray -> "${value.size} ${odmiana(value.size, "pozycja", "pozycje", "pozycji")}"
    is JsonObject -> null
    else -> null
}

private fun JsonElement.isEmpty(): Boolean = when (this) {
    JsonNull -> true
    is JsonPrimitive -> isString && content.isBlank()
    is JsonArray -> isEmpty()
    is JsonObject -> isEmpty()
}

private fun JsonObject?.at(path: List<String>): JsonElement? {
    var current: JsonElement? = this
    for (part in path) current = (current as? JsonObject)?.get(part)
    return current
}

private fun JsonObject.str(key: String): String? = (get(key) as? JsonPrimitive)?.takeUnless { it is JsonNull }?.contentOrNull?.trim()?.takeIf { it.isNotEmpty() }
private fun JsonObject.long(key: String): Long? = (get(key) as? JsonPrimitive)?.let { it.longOrNull ?: it.contentOrNull?.toLongOrNull() }
private fun JsonObject.double(key: String): Double? = (get(key) as? JsonPrimitive)?.let { it.doubleOrNull ?: it.contentOrNull?.toDoubleOrNull() }

// ---------------------------------------------------------------------------
// Zakladka "Podatnosci"
// ---------------------------------------------------------------------------

@Composable internal fun VulnerabilitiesTab(data: Vulnerabilities?) {
    val kolory = LocalCmdbColors.current
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading("Podatności")
        if (data == null) {
            InfoCard(Icons.Outlined.Info, "Brak danych", "Serwer nie przesłał informacji o podatnościach. Zaktualizuj serwer CMDB.")
            return@Column
        }
        // "Nieznany" to nie zero - mowimy wprost, ze nie sprawdzilismy.
        if (data.status != "ok") {
            InfoCard(Icons.Outlined.Info, "Nie sprawdzono", data.detail ?: "Nie udało się ustalić podatności tej maszyny.")
            return@Column
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            CountTile("Do naprawy", data.fixableCount, if ((data.fixableCount ?: 0) > 0) kolory.warn else kolory.ok, Modifier.weight(1f))
            CountTile("Krytyczne (CVSS ≥ 7)", data.criticalCount, if ((data.criticalCount ?: 0) > 0) kolory.danger else kolory.ok, Modifier.weight(1f))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            CountTile("Bez poprawki", data.openCount, MaterialTheme.colorScheme.onSurface, Modifier.weight(1f))
            CountTile("Drobne", data.minorCount, MaterialTheme.colorScheme.onSurfaceVariant, Modifier.weight(1f))
        }
        data.source?.let { Text("Dane: $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }

        if (data.packages.isNotEmpty()) {
            ExpandableCard("Pakiety do aktualizacji", data.packages.size.toString(), Icons.Outlined.Apps, "podatnosci-pakiety") {
                PagedList(data.packages, "podatnosci-pakiety") { p ->
                    ItemBox {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(p.packageName ?: "—", Modifier.weight(1f), fontWeight = FontWeight.SemiBold)
                            ScoreBadge(p.maxScore)
                        }
                        val opis = listOfNotNull(
                            "${p.count} ${odmiana(p.count, "luka", "luki", "luk")}",
                            p.critical.takeIf { it > 0 }?.let { "$it ${odmiana(it, "krytyczna", "krytyczne", "krytycznych")}" },
                        ).joinToString(" · ")
                        Text(opis, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.height(4.dp))
                        p.installedVersion?.let { KeyValueRow("Zainstalowana", it) }
                        p.fixedVersion?.let { KeyValueRow("Naprawiona w", it) }
                    }
                }
            }
        }
        val doNaprawy = data.entries.filter { it.status == "resolved" }
        val bezPoprawki = data.entries.filter { it.status != "resolved" && !it.minor }
        val drobne = data.entries.filter { it.status != "resolved" && it.minor }
        VulnerabilityGroup("Do naprawy", doNaprawy, Icons.Outlined.Security, "podatnosci-naprawa")
        VulnerabilityGroup("Bez poprawki", bezPoprawki, Icons.Outlined.ErrorOutline, "podatnosci-otwarte")
        VulnerabilityGroup("Drobne", drobne, Icons.Outlined.Info, "podatnosci-drobne")
        if (data.count == 0) InfoCard(Icons.Outlined.Security, "Brak znanych podatności", "Żaden zainstalowany pakiet nie ma zgłoszonych luk.")
        if (data.truncated > 0) {
            Text("Pokazano pierwsze ${data.entries.size} pozycji, pozostało ${data.truncated}. Pełna lista jest w panelu WWW.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable private fun VulnerabilityGroup(title: String, items: List<Vulnerability>, icon: ImageVector, key: String) {
    if (items.isEmpty()) return
    val uri = LocalUriHandler.current
    ExpandableCard(title, items.size.toString(), icon, key) {
        PagedList(items, key) { v ->
            val link = v.link
            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).then(
                if (link != null) Modifier.clickable { runCatching { uri.openUri(link) } } else Modifier,
            )) {
                ItemBox {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(v.cve, Modifier.weight(1f), fontWeight = FontWeight.SemiBold)
                        ScoreBadge(v.baseScore)
                    }
                    val wersje = listOfNotNull(v.installedVersion, v.fixedVersion?.let { "→ $it" }).joinToString(" ")
                    Text(listOfNotNull(v.packageName, wersje.ifBlank { null }).joinToString(" · "),
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    v.priority?.let { Text("Priorytet dystrybucji: $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                    v.summary?.let {
                        Spacer(Modifier.height(4.dp))
                        Text(it, style = MaterialTheme.typography.bodyMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
    }
}

@Composable private fun ScoreBadge(score: Double?) {
    val kolory = LocalCmdbColors.current
    val (text, color) = when {
        score == null -> "brak oceny" to MaterialTheme.colorScheme.onSurfaceVariant
        score >= 9.0 -> "${formatNumber(score)} krytyczna" to kolory.danger
        score >= 7.0 -> "${formatNumber(score)} wysoka" to kolory.danger
        score >= 4.0 -> "${formatNumber(score)} średnia" to kolory.warn
        else -> "${formatNumber(score)} niska" to kolory.ok
    }
    Plakietka(text, color)
}

// ---------------------------------------------------------------------------
// Zakladka "Aktualizacje"
// ---------------------------------------------------------------------------

@Composable internal fun UpdatesTab(report: JsonObject?) {
    val kolory = LocalCmdbColors.current
    val software = report?.get("software") as? JsonObject
    val pending = software?.get("updates_pending") as? JsonObject
    val installed = software?.get("updates") as? JsonArray
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading("Aktualizacje")
        when {
            pending == null -> InfoCard(Icons.Outlined.Info, "Nie sprawdzono oczekujących aktualizacji",
                "Agent nie przesłał tej informacji. Sprawdzanie może być wyłączone w ustawieniach agenta albo agent jest w starszej wersji.")
            pending.str("status") != "ok" -> InfoCard(Icons.Outlined.ErrorOutline, "Nie udało się sprawdzić",
                pending.str("detail") ?: "Agent nie dostał odpowiedzi od menedżera aktualizacji.")
            else -> {
                val count = pending.long("count")?.toInt()
                val security = pending.long("security_count")?.toInt()
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    CountTile("Czeka na instalację", count, if ((count ?: 0) > 0) kolory.warn else kolory.ok, Modifier.weight(1f))
                    CountTile("Poprawki bezpieczeństwa", security, if ((security ?: 0) > 0) kolory.danger else kolory.ok, Modifier.weight(1f))
                }
                val sprawdzono = pending.str("checked_at")?.let { formatPrimitive("checked_at", JsonPrimitive(it)) }
                val zrodlo = pending.str("source")
                Text(listOfNotNull(sprawdzono?.let { "Sprawdzono $it" }, zrodlo).joinToString(" · "),
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                val entries = (pending["entries"] as? JsonArray)?.mapNotNull { it as? JsonObject }.orEmpty()
                if (entries.isEmpty()) InfoCard(Icons.Outlined.SystemUpdate, "System jest aktualny", "Nie ma aktualizacji czekających na instalację.")
                else ExpandableCard("Oczekujące aktualizacje", entries.size.toString(), Icons.Outlined.SystemUpdate, "aktualizacje-oczekujace") {
                    PagedList(entries, "aktualizacje-oczekujace") { PendingUpdateRow(it) }
                }
            }
        }
        if (!installed.isNullOrEmpty()) {
            ExpandableCard("Zainstalowane aktualizacje", installed.size.toString(), Icons.Outlined.History, "aktualizacje-zainstalowane") {
                JsonBody(installed, "updates")
            }
        }
    }
}

@Composable private fun PendingUpdateRow(update: JsonObject) {
    val kolory = LocalCmdbColors.current
    val title = update.str("title") ?: update.str("id") ?: "Aktualizacja"
    ItemBox {
        Text(title, fontWeight = FontWeight.SemiBold)
        val id = update.str("id")?.takeIf { it != title }
        val wersje = listOfNotNull(update.str("current_version"), update.str("new_version")?.let { "→ $it" }).joinToString(" ")
        val opis = listOfNotNull(id, wersje.ifBlank { null }, update.str("source_repo")).joinToString(" · ")
        if (opis.isNotEmpty()) Text(opis, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        val security = (update["security"] as? JsonPrimitive)?.booleanOrNull == true
        val severity = update.str("severity")
        if (security || severity != null) {
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                if (security) Plakietka("bezpieczeństwo", kolory.danger)
                severity?.let { Plakietka(it, kolory.warn) }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Drobne elementy wspolne dla zakladek
// ---------------------------------------------------------------------------

/** Lista z przyciskiem "Pokaż więcej" - pierwsze 15 pozycji, potem po 30. */
@Composable private fun <T> PagedList(items: List<T>, key: String, row: @Composable (T) -> Unit) {
    var limit by rememberSaveable(key, items.size) { mutableStateOf(PAGE) }
    items.take(limit).forEach { row(it) }
    if (items.size > limit) {
        TextButton(onClick = { limit += PAGE * 2 }) { Text("Pokaż więcej (${items.size - limit} pozostało)") }
    }
}

@Composable private fun CountTile(label: String, value: Int?, color: Color, modifier: Modifier) {
    ElevatedCmdbCard(modifier) {
        Text(value?.toString() ?: "—", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold, color = color)
        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun InfoCard(icon: ImageVector, title: String, text: String) {
    ElevatedCmdbCard {
        Row(verticalAlignment = Alignment.Top) {
            Icon(icon, null, tint = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.width(12.dp))
            Column {
                Text(title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable private fun Plakietka(text: String, color: Color) {
    Text(
        text,
        Modifier.clip(RoundedCornerShape(20.dp)).background(color.copy(alpha = .16f)).padding(horizontal = 8.dp, vertical = 3.dp),
        color = color,
        style = MaterialTheme.typography.labelSmall,
        fontWeight = FontWeight.Bold,
    )
}

private fun odmiana(n: Int, jeden: String, kilka: String, wiele: String): String = when {
    n == 1 -> jeden
    n % 10 in 2..4 && n % 100 !in 12..14 -> kilka
    else -> wiele
}
