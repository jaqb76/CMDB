package pl.hubzso.cmdb.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.Email
import androidx.compose.material.icons.outlined.Person
import androidx.compose.material.icons.outlined.Save
import androidx.compose.material.icons.outlined.Search
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
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import pl.hubzso.cmdb.data.DictionaryCategory
import pl.hubzso.cmdb.data.DictionaryEntry
import pl.hubzso.cmdb.data.DictionaryField
import pl.hubzso.cmdb.data.DictionarySchema

@Composable
internal fun VisualDictionariesScreen(
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
    val initial = categories.firstOrNull { it.key == "osoba" }?.key ?: categories.firstOrNull()?.key.orEmpty()
    var category by remember(categories) { mutableStateOf(initial) }
    var query by remember { mutableStateOf("") }
    var edited by remember { mutableStateOf<DictionaryEntry?>(null) }
    var creating by remember { mutableStateOf(false) }
    LaunchedEffect(mutationVersion) { creating = false; edited = null }
    val schema = schemas[category]
    if ((creating || edited != null) && schema != null) {
        VisualDictionaryEditor(schema, edited, dictionaries, padding, saving, error, { creating = false; edited = null }, onDelete) { id, values ->
            onSave(category, id, values)
        }
        return
    }
    val entries = dictionaries[category].orEmpty().filter {
        query.isBlank() || it.value.contains(query, true) || it.attributes.values.any { value -> value.toString().contains(query, true) }
    }
    Box(Modifier.fillMaxSize().padding(padding)) {
        LazyColumn(
            Modifier.fillMaxSize(),
            contentPadding = PaddingValues(start = 14.dp, end = 14.dp, top = 14.dp, bottom = 92.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it },
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text(if (category == "osoba") "Szukaj osoby" else "Szukaj wpisu") },
                    leadingIcon = { Icon(Icons.Outlined.Search, null) },
                    singleLine = true,
                    shape = RoundedCornerShape(14.dp),
                )
            }
            item {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(categories, key = { it.key }) { categoryItem ->
                        CmdbFilterChip(categoryItem.label, category == categoryItem.key) { category = categoryItem.key; query = "" }
                    }
                }
            }
            item { Text("${entries.size} ${if (category == "osoba") "osób" else "wpisów"}", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant) }
            items(entries, key = { it.id }) { entry ->
                DictionaryPersonCard(entry, schema, dictionaries, canWrite) { edited = entry }
            }
            if (entries.isEmpty()) item { EmptyState("Brak wpisów spełniających kryteria") }
        }
        if (canWrite) FloatingActionButton(
            onClick = { creating = true },
            modifier = Modifier.align(Alignment.BottomEnd).padding(20.dp),
            containerColor = MaterialTheme.colorScheme.primary,
            contentColor = Color.White,
        ) { Icon(Icons.Outlined.Add, "Dodaj wpis") }
    }
}

@Composable private fun DictionaryPersonCard(
    entry: DictionaryEntry,
    schema: DictionarySchema?,
    dictionaries: Map<String, List<DictionaryEntry>>,
    clickable: Boolean,
    onClick: () -> Unit,
) {
    val details = schema?.fields.orEmpty().mapNotNull { field ->
        val raw = (entry.attributes[field.key] as? JsonPrimitive)?.contentOrNull
        if (raw.isNullOrBlank()) null else {
            val shown = if (field.type == "odwolanie") dictionaries[field.target]?.firstOrNull { it.id == raw }?.value ?: raw else raw
            field to shown
        }
    }
    val email = details.firstOrNull { it.first.format == "email" || it.first.key.contains("email", true) }?.second
    val secondary = details.firstOrNull { it.second != email }?.let { "${it.first.label}: ${it.second}" }
    ElevatedCmdbCard(Modifier.clickable(enabled = clickable, onClick = onClick)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val colors = listOf(BrandBlue, Color(0xFF8B6CE5), SuccessGreen, WarningAmber, DangerRed)
            val avatarColor = colors[kotlin.math.abs(entry.id.hashCode()) % colors.size]
            Box(Modifier.size(50.dp).clip(CircleShape).background(avatarColor.copy(alpha = .16f)), contentAlignment = Alignment.Center) {
                Text(initials(entry.value), color = avatarColor, fontWeight = FontWeight.ExtraBold)
            }
            Spacer(Modifier.width(13.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(entry.value, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                secondary?.let { Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1) }
                email?.let {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Outlined.Email, null, Modifier.size(14.dp), MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.width(5.dp))
                        Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
                    }
                }
            }
            Icon(Icons.Outlined.Person, null, tint = MaterialTheme.colorScheme.primary)
        }
    }
}

private fun initials(value: String): String = value.split(' ').filter { it.isNotBlank() }.take(2).mapNotNull { it.firstOrNull()?.uppercase() }.joinToString("").ifBlank { "?" }

@Composable private fun VisualDictionaryEditor(
    schema: DictionarySchema,
    entry: DictionaryEntry?,
    dictionaries: Map<String, List<DictionaryEntry>>,
    padding: PaddingValues,
    saving: Boolean,
    error: String?,
    onDismiss: () -> Unit,
    onDelete: (String, String) -> Unit,
    onSave: (String?, Map<String, JsonElement>) -> Unit,
) {
    val values = remember(schema, entry) {
        mutableStateMapOf<String, String>().apply {
            schema.fields.forEach { put(it.key, entry?.attributes?.get(it.key)?.jsonPrimitive?.contentOrNull.orEmpty()) }
        }
    }
    var confirmDelete by remember { mutableStateOf(false) }
    BackHandler(enabled = !saving, onBack = onDismiss)
    if (confirmDelete && entry != null) AlertDialog(
        onDismissRequest = { if (!saving) confirmDelete = false },
        title = { Text("Usunąć wpis?") },
        text = { Text("Usunięcie ${entry.value} może odłączyć ten wpis od przypisanych maszyn.") },
        confirmButton = { Button(enabled = !saving, onClick = { onDelete(schema.category, entry.id) }) { Text("Usuń") } },
        dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Anuluj") } },
    )
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            Text(if (entry == null) "Nowy wpis" else "Edycja wpisu", style = MaterialTheme.typography.headlineMedium)
            Text(schema.label, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
        schema.fields.groupBy { it.group }.forEach { (group, fields) ->
            item {
                ElevatedCmdbCard {
                    Text(group, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.height(12.dp))
                    fields.forEachIndexed { index, field ->
                        VisualDynamicField(field, values[field.key].orEmpty(), dictionaries) { values[field.key] = it }
                        if (index != fields.lastIndex) Spacer(Modifier.height(12.dp))
                    }
                }
            }
        }
        item {
            Button(
                enabled = !saving && schema.fields.filter { it.required }.all { !values[it.key].isNullOrBlank() },
                onClick = {
                    onSave(entry?.id, values.mapValues { (key, value) ->
                        if (schema.fields.first { it.key == key }.type == "logiczna") JsonPrimitive(value == "true" || value == "1") else JsonPrimitive(value)
                    })
                },
                modifier = Modifier.fillMaxWidth().height(52.dp),
            ) { Icon(Icons.Outlined.Save, null); Spacer(Modifier.width(8.dp)); Text("ZAPISZ") }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(enabled = !saving, onClick = onDismiss, modifier = Modifier.weight(1f)) { Text("ANULUJ") }
                if (entry != null) OutlinedButton(enabled = !saving, onClick = { confirmDelete = true }, modifier = Modifier.weight(1f)) {
                    Icon(Icons.Outlined.Delete, null, tint = DangerRed); Spacer(Modifier.width(6.dp)); Text("USUŃ", color = DangerRed)
                }
            }
        }
    }
}

@Composable private fun VisualDynamicField(
    field: DictionaryField,
    value: String,
    dictionaries: Map<String, List<DictionaryEntry>>,
    onChange: (String) -> Unit,
) {
    val label = field.label + if (field.required) " *" else ""
    when (field.type) {
        "logiczna" -> Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Text(label); Switch(checked = value == "true" || value == "1", onCheckedChange = { onChange(it.toString()) })
        }
        "wybor" -> ModernChoiceField(label, value, field.options.map { it to it }, onChange)
        "odwolanie" -> ModernChoiceField(label, value, dictionaries[field.target].orEmpty().map { it.id to it.value }, onChange)
        else -> OutlinedTextField(
            value = value,
            onValueChange = onChange,
            label = { Text(label) },
            supportingText = { if (!field.hint.isNullOrBlank()) Text(field.hint) },
            keyboardOptions = KeyboardOptions(keyboardType = when {
                field.type == "liczba" -> KeyboardType.Decimal
                field.format == "email" -> KeyboardType.Email
                else -> KeyboardType.Text
            }),
            modifier = Modifier.fillMaxWidth(),
            minLines = if (field.type == "notatka") 3 else 1,
            shape = RoundedCornerShape(12.dp),
        )
    }
}
