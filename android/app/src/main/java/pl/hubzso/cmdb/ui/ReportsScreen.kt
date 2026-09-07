package pl.hubzso.cmdb.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import pl.hubzso.cmdb.data.ReportCatalog
import pl.hubzso.cmdb.data.ReportDefinition
import pl.hubzso.cmdb.data.ReportOption
import pl.hubzso.cmdb.data.ReportWrite

@Composable
fun ReportManagementScreen(
    reports: List<ReportDefinition>,
    catalog: ReportCatalog?,
    padding: PaddingValues,
    canWrite: Boolean,
    saving: Boolean,
    mutationVersion: Int,
    error: String?,
    onSend: (String) -> Unit,
    onSave: (String?, ReportWrite) -> Unit,
    onDelete: (String) -> Unit,
) {
    var edited by remember { mutableStateOf<ReportDefinition?>(null) }
    var creating by remember { mutableStateOf(false) }
    LaunchedEffect(mutationVersion) { creating = false; edited = null }
    var sending by remember { mutableStateOf<ReportDefinition?>(null) }
    if (sending != null) AlertDialog(
        onDismissRequest = { sending = null }, title = { Text("Wysłać raport teraz?") },
        text = { Text("Raport ${sending!!.name} zostanie wysłany do: ${sending!!.recipients}" + if (sending!!.sendToVendors) " oraz właściwych dostawców." else ".") },
        confirmButton = { Button(enabled = !saving, onClick = { onSend(sending!!.id); sending = null }) { Text("Wyślij") } },
        dismissButton = { TextButton(onClick = { sending = null }) { Text("Anuluj") } },
    )
    if ((creating || edited != null) && catalog != null) {
        ReportEditor(edited, catalog, saving, error, { creating = false; edited = null }, onDelete) { id, body ->
            onSave(id, body)
        }
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        if (canWrite) item { Button(onClick = { creating = true }, modifier = Modifier.fillMaxWidth()) { Text("Utwórz raport") } }
        items(reports, key = { it.id }) { report ->
            Card(Modifier.fillMaxWidth().clickable(enabled = canWrite) { edited = report }) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(report.name, fontWeight = FontWeight.SemiBold)
                        Text(if (report.active) "Aktywny" else "Wyłączony", color = if (report.active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text("${report.type} • ${report.frequency}", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(report.recipients)
                    report.lastStatus?.let { Text("Ostatnia wysyłka: $it${report.lastSentAt?.let { date -> " • $date" } ?: ""}") }
                    if (canWrite) OutlinedButton(enabled = !saving, onClick = { sending = report }, modifier = Modifier.fillMaxWidth()) { Text("Wyślij teraz") }
                }
            }
        }
    }
}

@Composable
private fun ReportEditor(
    report: ReportDefinition?,
    catalog: ReportCatalog,
    saving: Boolean,
    error: String?,
    onDismiss: () -> Unit,
    onDelete: (String) -> Unit,
    onSave: (String?, ReportWrite) -> Unit,
) {
    var name by remember(report) { mutableStateOf(report?.name.orEmpty()) }
    var type by remember(report) { mutableStateOf(report?.type ?: catalog.types.firstOrNull()?.key.orEmpty()) }
    var frequency by remember(report) { mutableStateOf(report?.frequency ?: "tygodniowo") }
    var recipients by remember(report) { mutableStateOf(report?.recipients.orEmpty()) }
    var active by remember(report) { mutableStateOf(report?.active ?: true) }
    var vendors by remember(report) { mutableStateOf(report?.sendToVendors ?: false) }
    val selectedColumns = remember(report, catalog) { mutableStateListOf<String>().apply {
        addAll(report?.columns?.takeIf { it.isNotEmpty() } ?: catalog.columns.filter { it.default }.map { it.key })
    } }
    var confirmDelete by remember { mutableStateOf(false) }
    if (confirmDelete && report != null) AlertDialog(
        onDismissRequest = { if (!saving) confirmDelete = false }, title = { Text("Usunąć raport ${report.name}?") },
        text = { Text("Zaplanowane wysyłki tego raportu zostaną usunięte.") },
        confirmButton = { Button(enabled = !saving, onClick = { onDelete(report.id) }) { Text("Usuń") } },
        dismissButton = { TextButton(enabled = !saving, onClick = { confirmDelete = false }) { Text("Anuluj") } },
    )
    AlertDialog(
        onDismissRequest = { if (!saving) onDismiss() },
        title = { Text(if (report == null) "Nowy raport" else "Edytuj raport") },
        text = {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
                item { OutlinedTextField(name, { name = it }, label = { Text("Nazwa") }, modifier = Modifier.fillMaxWidth()) }
                item { ReportChoice("Rodzaj", type, catalog.types) { type = it } }
                item { ReportChoice("Częstotliwość", frequency, catalog.frequencies) { frequency = it } }
                item { OutlinedTextField(recipients, { recipients = it }, label = { Text("Adresaci") }, supportingText = { Text("Adresy oddziel przecinkami lub średnikami") }, modifier = Modifier.fillMaxWidth(), minLines = 2) }
                item { Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) { Text("Raport aktywny"); Switch(active, { active = it }) } }
                item { Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) { Text("Wyślij także do dostawców"); Switch(vendors, { vendors = it }) } }
                item { Text("Kolumny raportu", style = MaterialTheme.typography.titleMedium) }
                items(catalog.columns, key = { it.key }) { column ->
                    FilterChip(
                        selected = column.key in selectedColumns,
                        onClick = { if (column.key in selectedColumns) selectedColumns.remove(column.key) else selectedColumns.add(column.key) },
                        label = { Text("${column.group}: ${column.label}") },
                    )
                }
            }
        },
        confirmButton = { Button(
            enabled = !saving && name.isNotBlank() && recipients.isNotBlank() && type.isNotBlank(),
            onClick = { onSave(report?.id, ReportWrite(name.trim(), type, frequency, recipients.trim(), active, vendors, selectedColumns.toList())) },
        ) { Text("Zapisz") } },
        dismissButton = { Row {
            if (report != null) TextButton(enabled = !saving, onClick = { confirmDelete = true }) { Text("Usuń") }
            TextButton(enabled = !saving, onClick = onDismiss) { Text("Anuluj") }
        } },
    )
}

@Composable private fun ReportChoice(label: String, value: String, options: List<ReportOption>, onChange: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val shown = options.firstOrNull { it.key == value }?.label ?: value
    Box(Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth()) { Text("$label: $shown") }
        DropdownMenu(expanded, { expanded = false }) {
            options.forEach { option -> DropdownMenuItem(text = { Text(option.label) }, onClick = { onChange(option.key); expanded = false }) }
        }
    }
}
