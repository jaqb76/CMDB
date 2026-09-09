package pl.hubzso.cmdb.ui

import androidx.activity.compose.BackHandler
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.Assessment
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.Email
import androidx.compose.material.icons.outlined.PlayArrow
import androidx.compose.material.icons.outlined.Save
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import pl.hubzso.cmdb.data.ReportCatalog
import pl.hubzso.cmdb.data.ReportDefinition
import pl.hubzso.cmdb.data.ReportWrite

@Composable
internal fun VisualReportManagementScreen(
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
    var sending by remember { mutableStateOf<ReportDefinition?>(null) }
    LaunchedEffect(mutationVersion) { creating = false; edited = null }
    sending?.let { report ->
        AlertDialog(
            onDismissRequest = { sending = null },
            title = { Text("Wysłać raport teraz?") },
            text = { Text("Raport ${report.name} zostanie wysłany do: ${report.recipients}.") },
            confirmButton = { Button(enabled = !saving, onClick = { onSend(report.id); sending = null }) { Text("Wyślij") } },
            dismissButton = { TextButton(onClick = { sending = null }) { Text("Anuluj") } },
        )
    }
    if ((creating || edited != null) && catalog != null) {
        VisualReportEditor(edited, catalog, padding, saving, error, { creating = false; edited = null }, onDelete, onSave)
        return
    }
    Box(Modifier.fillMaxSize().padding(padding)) {
        LazyColumn(
            Modifier.fillMaxSize(),
            contentPadding = PaddingValues(start = 14.dp, end = 14.dp, top = 14.dp, bottom = 92.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item { Text("Zaplanowane raporty", style = MaterialTheme.typography.headlineMedium); Text("Automatyczne zestawienia i wysyłki", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            items(reports, key = { it.id }) { report ->
                ElevatedCmdbCard(Modifier.clickable(enabled = canWrite) { edited = report }) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RoundIcon(Icons.Outlined.Assessment, if (report.active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.width(12.dp))
                        Column(Modifier.weight(1f)) {
                            Text(report.name, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            Text("${report.type} • ${report.frequency}", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        StatusPill(if (report.active) "Aktywny" else "Wyłączony", if (report.active) LocalCmdbColors.current.ok else MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Spacer(Modifier.height(12.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Outlined.Email, null, Modifier.size(17.dp), MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.width(7.dp))
                        Text(report.recipients, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
                    }
                    report.lastSentAt?.let {
                        Spacer(Modifier.height(6.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Outlined.Schedule, null, Modifier.size(17.dp), MaterialTheme.colorScheme.onSurfaceVariant)
                            Spacer(Modifier.width(7.dp)); Text("Ostatnia wysyłka: $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                    if (canWrite) {
                        Spacer(Modifier.height(12.dp))
                        OutlinedButton(onClick = { sending = report }, enabled = !saving, modifier = Modifier.fillMaxWidth()) {
                            Icon(Icons.Outlined.PlayArrow, null); Spacer(Modifier.width(6.dp)); Text("WYŚLIJ TERAZ")
                        }
                    }
                }
            }
            if (reports.isEmpty()) item { EmptyState("Nie utworzono jeszcze żadnego raportu") }
        }
        if (canWrite && catalog != null) FloatingActionButton(
            onClick = { creating = true },
            modifier = Modifier.align(Alignment.BottomEnd).padding(20.dp),
            containerColor = MaterialTheme.colorScheme.primary,
            contentColor = Color.White,
        ) { Icon(Icons.Outlined.Add, "Nowy raport") }
    }
}

@Composable private fun VisualReportEditor(
    report: ReportDefinition?,
    catalog: ReportCatalog,
    padding: PaddingValues,
    saving: Boolean,
    error: String?,
    onDismiss: () -> Unit,
    onDelete: (String) -> Unit,
    onSave: (String?, ReportWrite) -> Unit,
) {
    var name by remember(report) { mutableStateOf(report?.name.orEmpty()) }
    var type by remember(report) { mutableStateOf(report?.type ?: catalog.types.firstOrNull()?.key.orEmpty()) }
    var frequency by remember(report) { mutableStateOf(report?.frequency ?: catalog.frequencies.firstOrNull()?.key.orEmpty()) }
    var recipients by remember(report) { mutableStateOf(report?.recipients.orEmpty()) }
    var active by remember(report) { mutableStateOf(report?.active ?: true) }
    var vendors by remember(report) { mutableStateOf(report?.sendToVendors ?: false) }
    val selectedColumns = remember(report, catalog) { mutableStateListOf<String>().apply { addAll(report?.columns?.takeIf { it.isNotEmpty() } ?: catalog.columns.filter { it.default }.map { it.key }) } }
    var confirmDelete by remember { mutableStateOf(false) }
    BackHandler(enabled = !saving, onBack = onDismiss)
    if (confirmDelete && report != null) AlertDialog(
        onDismissRequest = { if (!saving) confirmDelete = false },
        title = { Text("Usunąć raport?") },
        text = { Text("Zaplanowane wysyłki raportu ${report.name} zostaną usunięte.") },
        confirmButton = { Button(enabled = !saving, onClick = { onDelete(report.id) }) { Text("Usuń") } },
        dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Anuluj") } },
    )
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { Text(if (report == null) "Nowy raport" else "Edytuj raport", style = MaterialTheme.typography.headlineMedium); Text("Ustaw harmonogram i zawartość", color = MaterialTheme.colorScheme.onSurfaceVariant) }
        error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
        item {
            ElevatedCmdbCard {
                Text("Podstawowe informacje", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(name, { name = it }, label = { Text("Nazwa raportu") }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp))
                Spacer(Modifier.height(12.dp))
                ModernChoiceField("Rodzaj", type, catalog.types.map { it.key to it.label }) { type = it }
                Spacer(Modifier.height(12.dp))
                ModernChoiceField("Częstotliwość", frequency, catalog.frequencies.map { it.key to it.label }) { frequency = it }
            }
        }
        item {
            ElevatedCmdbCard {
                Text("Odbiorcy", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(recipients, { recipients = it }, label = { Text("Adresy e-mail") }, supportingText = { Text("Oddziel adresy przecinkami lub średnikami") }, modifier = Modifier.fillMaxWidth(), minLines = 2, shape = RoundedCornerShape(12.dp))
                SettingSwitch("Raport aktywny", active) { active = it }
                SettingSwitch("Wyślij także do dostawców", vendors) { vendors = it }
            }
        }
        item { Text("Kolumny raportu", style = MaterialTheme.typography.titleLarge) }
        catalog.columns.groupBy { it.group }.forEach { (group, columns) ->
            item {
                ElevatedCmdbCard {
                    Text(group, fontWeight = FontWeight.SemiBold)
                    Spacer(Modifier.height(8.dp))
                    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        items(columns, key = { it.key }) { column ->
                            CmdbFilterChip(column.label, column.key in selectedColumns) {
                                if (column.key in selectedColumns) selectedColumns.remove(column.key) else selectedColumns.add(column.key)
                            }
                        }
                    }
                }
            }
        }
        item {
            Button(
                enabled = !saving && name.isNotBlank() && recipients.isNotBlank() && type.isNotBlank(),
                onClick = { onSave(report?.id, ReportWrite(name.trim(), type, frequency, recipients.trim(), active, vendors, selectedColumns.toList())) },
                modifier = Modifier.fillMaxWidth().height(52.dp),
            ) { Icon(Icons.Outlined.Save, null); Spacer(Modifier.width(7.dp)); Text("ZAPISZ RAPORT") }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(onClick = onDismiss, enabled = !saving, modifier = Modifier.weight(1f)) { Text("ANULUJ") }
                if (report != null) OutlinedButton(onClick = { confirmDelete = true }, enabled = !saving, modifier = Modifier.weight(1f)) {
                    val czerwien = LocalCmdbColors.current.danger
                    Icon(Icons.Outlined.Delete, null, tint = czerwien); Spacer(Modifier.width(5.dp)); Text("USUŃ", color = czerwien)
                }
            }
        }
    }
}

@Composable private fun SettingSwitch(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(top = 12.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Text(label); Switch(checked = checked, onCheckedChange = onChange)
    }
}
