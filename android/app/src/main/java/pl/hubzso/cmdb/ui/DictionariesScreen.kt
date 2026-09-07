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
import androidx.compose.foundation.lazy.LazyRow
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import pl.hubzso.cmdb.data.AssetSummary
import pl.hubzso.cmdb.data.AssignmentWrite
import pl.hubzso.cmdb.data.DictionaryCategory
import pl.hubzso.cmdb.data.DictionaryEntry
import pl.hubzso.cmdb.data.DictionaryField
import pl.hubzso.cmdb.data.DictionarySchema

@Composable
fun DictionariesScreen(
    categories: List<DictionaryCategory>,
    schemas: Map<String, DictionarySchema>,
    dictionaries: Map<String, List<DictionaryEntry>>,
    canWrite: Boolean,
    padding: PaddingValues,
    saving: Boolean,
    mutationVersion: Int,
    error: String?,
    onSave: (String, String?, Map<String, JsonElement>) -> Unit,
    onDelete: (String, String) -> Unit,
) {
    var category by remember(categories) { mutableStateOf(categories.firstOrNull()?.key.orEmpty()) }
    var edited by remember { mutableStateOf<DictionaryEntry?>(null) }
    var creating by remember { mutableStateOf(false) }
    LaunchedEffect(mutationVersion) { creating = false; edited = null }
    val schema = schemas[category]
    if ((creating || edited != null) && schema != null) {
        DictionaryEditor(schema, edited, dictionaries, saving, error, { creating = false; edited = null }, onDelete) { id, values ->
            onSave(category, id, values)
        }
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(categories, key = { it.key }) { item ->
                    FilterChip(selected = category == item.key, onClick = { category = item.key }, label = { Text(item.label) })
                }
            }
        }
        if (canWrite) item { Button(onClick = { creating = true }, modifier = Modifier.fillMaxWidth()) { Text("Dodaj wpis") } }
        items(dictionaries[category].orEmpty(), key = { it.id }) { entry ->
            Card(Modifier.fillMaxWidth().clickable(enabled = canWrite) { edited = entry }) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(entry.value, fontWeight = FontWeight.SemiBold)
                    val details = schemas[category]?.fields.orEmpty().mapNotNull { field ->
                        val raw = (entry.attributes[field.key] as? JsonPrimitive)?.contentOrNull
                        if (raw.isNullOrBlank()) null else {
                            val value = if (field.type == "odwolanie") dictionaries[field.target]?.firstOrNull { it.id == raw }?.value ?: "Brak powiązania"
                                else if (field.type == "logiczna") (if (raw == "true") "Tak" else "Nie") else raw
                            "${field.label}: $value"
                        }
                    }
                    if (details.isNotEmpty()) Text(details.joinToString(" • "), color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
private fun DictionaryEditor(
    schema: DictionarySchema,
    entry: DictionaryEntry?,
    dictionaries: Map<String, List<DictionaryEntry>>,
    saving: Boolean,
    error: String?,
    onDismiss: () -> Unit,
    onDelete: (String, String) -> Unit,
    onSave: (String?, Map<String, JsonElement>) -> Unit,
) {
    val values = remember(schema, entry) {
        mutableStateMapOf<String, String>().apply {
            schema.fields.forEach { field -> put(field.key, entry?.attributes?.get(field.key)?.jsonPrimitive?.contentOrNull.orEmpty()) }
        }
    }
    var confirmDelete by remember { mutableStateOf(false) }
    if (confirmDelete && entry != null) AlertDialog(
        onDismissRequest = { if (!saving) confirmDelete = false },
        title = { Text("Usunąć wpis?") },
        text = { Text("Usunięcie ${entry.value} odłączy ten wpis od przypisanych maszyn.") },
        confirmButton = { Button(enabled = !saving, onClick = { onDelete(schema.category, entry.id) }) { Text("Usuń") } },
        dismissButton = { TextButton(enabled = !saving, onClick = { confirmDelete = false }) { Text("Anuluj") } },
    )
    AlertDialog(
        onDismissRequest = { if (!saving) onDismiss() },
        title = { Text(if (entry == null) "Nowy wpis: ${schema.label}" else entry.value) },
        text = {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
                schema.fields.groupBy { it.group }.forEach { (group, fields) ->
                    item { Text(group, style = MaterialTheme.typography.titleSmall) }
                    items(fields, key = { it.key }) { field -> DynamicField(field, values[field.key].orEmpty(), dictionaries) { values[field.key] = it } }
                }
            }
        },
        confirmButton = { Button(enabled = !saving && schema.fields.filter { it.required }.all { !values[it.key].isNullOrBlank() },
            onClick = { onSave(entry?.id, values.mapValues { (key, value) ->
                if (schema.fields.first { it.key == key }.type == "logiczna") JsonPrimitive(value == "true" || value == "1") else JsonPrimitive(value)
            }) }) { Text("Zapisz") } },
        dismissButton = {
            Row {
                if (entry != null) TextButton(enabled = !saving, onClick = { confirmDelete = true }) { Text("Usuń") }
                TextButton(enabled = !saving, onClick = onDismiss) { Text("Anuluj") }
            }
        },
    )
}

@Composable
private fun DynamicField(field: DictionaryField, value: String, dictionaries: Map<String, List<DictionaryEntry>>, onChange: (String) -> Unit) {
    val label = field.label + if (field.required) " *" else ""
    when (field.type) {
        "logiczna" -> Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label); Switch(checked = value == "true" || value == "1", onCheckedChange = { onChange(it.toString()) })
        }
        "wybor" -> ChoiceStringField(label, value, field.options, onChange)
        "odwolanie" -> ChoiceField(label, value, dictionaries[field.target].orEmpty().map { it.id to it.value }, onChange)
        else -> OutlinedTextField(value, onChange, label = { Text(label) }, supportingText = { Text(field.hint ?: if (field.type == "data") "RRRR-MM-DD" else if (field.type == "liczba") "${field.min?.let { "Min: $it " }.orEmpty()}${field.max?.let { "Max: $it" }.orEmpty()}" else "") },
            keyboardOptions = KeyboardOptions(keyboardType = when {
                field.type == "liczba" -> KeyboardType.Decimal
                field.format == "email" -> KeyboardType.Email
                else -> KeyboardType.Text
            }), modifier = Modifier.fillMaxWidth(), minLines = if (field.type == "notatka") 3 else 1)
    }
}

@Composable private fun ChoiceStringField(label: String, value: String, options: List<String>, onChange: (String) -> Unit) =
    ChoiceField(label, value, options.map { it to it }, onChange)

@Composable private fun ChoiceField(label: String, value: String, options: List<Pair<String, String>>, onChange: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val shown = options.firstOrNull { it.first == value }?.second ?: value.ifBlank { "Wybierz" }
    Box(Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth()) { Text("$label: $shown") }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(text = { Text("— brak —") }, onClick = { onChange(""); expanded = false })
            options.forEach { (key, text) -> DropdownMenuItem(text = { Text(text) }, onClick = { onChange(key); expanded = false }) }
        }
    }
}

@Composable
fun AssignmentDialog(
    asset: AssetSummary,
    dictionaries: Map<String, List<DictionaryEntry>>,
    saving: Boolean,
    error: String?,
    onDismiss: () -> Unit,
    onSave: (AssignmentWrite) -> Unit,
) {
    var owner by remember { mutableStateOf(asset.owner?.id.orEmpty()) }
    var user by remember { mutableStateOf(asset.user?.id.orEmpty()) }
    var location by remember { mutableStateOf(asset.location?.id.orEmpty()) }
    var role by remember { mutableStateOf(asset.roleLabel.orEmpty()) }
    var place by remember { mutableStateOf(asset.place.orEmpty()) }
    val people = dictionaries["osoba"].orEmpty().map { it.id to it.value }
    val locations = dictionaries["lokalizacja"].orEmpty().map { it.id to it.value }
    AlertDialog(
        onDismissRequest = { if (!saving) onDismiss() },
        title = { Text("Przypisania: ${asset.hostname}") },
        text = { Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            ChoiceField("Opiekun", owner, people) { owner = it }
            ChoiceField("Użytkownik", user, people) { user = it }
            ChoiceField("Lokalizacja", location, locations) { location = it }
            OutlinedTextField(role, { role = it }, label = { Text("Rola") }, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(place, { place = it }, label = { Text("Miejsce") }, modifier = Modifier.fillMaxWidth())
        } },
        confirmButton = { Button(enabled = !saving, onClick = { onSave(AssignmentWrite(owner.ifBlank { null }, user.ifBlank { null }, location.ifBlank { null }, role.ifBlank { null }, place.ifBlank { null })) }) { Text("Zapisz") } },
        dismissButton = { TextButton(enabled = !saving, onClick = onDismiss) { Text("Anuluj") } },
    )
}
