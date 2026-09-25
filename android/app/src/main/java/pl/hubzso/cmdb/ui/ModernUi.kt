package pl.hubzso.cmdb.ui

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.automirrored.outlined.Logout
import androidx.compose.material.icons.automirrored.outlined.MenuBook
import androidx.compose.material.icons.outlined.Assessment
import androidx.compose.material.icons.outlined.Badge
import androidx.compose.material.icons.outlined.Business
import androidx.compose.material.icons.outlined.ChevronRight
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.DarkMode
import androidx.compose.material.icons.outlined.Dns
import androidx.compose.material.icons.outlined.Email
import androidx.compose.material.icons.outlined.Fingerprint
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.Home
import androidx.compose.material.icons.outlined.KeyboardArrowDown
import androidx.compose.material.icons.outlined.LightMode
import androidx.compose.material.icons.outlined.LocationOn
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.MoreHoriz
import androidx.compose.material.icons.outlined.MoreVert
import androidx.compose.material.icons.outlined.NotificationsNone
import androidx.compose.material.icons.outlined.Person
import androidx.compose.material.icons.outlined.PersonOff
import androidx.compose.material.icons.outlined.Refresh
import androidx.compose.material.icons.outlined.Save
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.Storage
import androidx.compose.material.icons.outlined.SupportAgent
import androidx.compose.material.icons.outlined.SyncProblem
import androidx.compose.material.icons.outlined.Visibility
import androidx.compose.material.icons.outlined.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import kotlin.math.cos
import kotlin.math.sin
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import pl.hubzso.cmdb.R
import pl.hubzso.cmdb.data.AssetDetail
import pl.hubzso.cmdb.data.AssetSummary
import pl.hubzso.cmdb.data.AssignmentWrite
import pl.hubzso.cmdb.data.ChangeEntry
import pl.hubzso.cmdb.data.CountItem
import pl.hubzso.cmdb.data.Dashboard
import pl.hubzso.cmdb.data.DictionaryEntry
import pl.hubzso.cmdb.data.NewMessage
import pl.hubzso.cmdb.data.NewTicket
import pl.hubzso.cmdb.data.ReportWrite
import pl.hubzso.cmdb.data.TicketAttachment
import pl.hubzso.cmdb.data.WybranyPlik

// Granat jest kolorem marki i w motywie jasnym pelni role koloru wiodacego:
// pasek gorny, przyciski, zaznaczone filtry. W motywie ciemnym granat zlewa
// sie z tlem, wiec role wiodaca przejmuje jasny blekit.
internal val BrandNavy = Color(0xFF0B3C6E)
internal val BrandBlue = Color(0xFF2E9BF5)
internal val BrandCyan = Color(0xFF29A0F0)
internal val SuccessGreen = Color(0xFF23A455)
internal val WarningAmber = Color(0xFFEE9F1B)
internal val DangerRed = Color(0xFFDE3B40)

private val LightBackground = Color(0xFFF2F4F7)
private val DarkBackground = Color(0xFF0A1A2B)
private val DarkSurface = Color(0xFF12263C)

/**
 * Barwy, ktorych Material nie ma w swoim schemacie, a ktore musza byc inne w
 * kazdym z motywow. Trzymamy je obok schematu zamiast liczyc jasnosc tla w
 * miejscu uzycia - inaczej ta sama kropka statusu wyszlaby raz zielona, raz
 * ledwo widoczna.
 */
internal data class CmdbColors(
    val ok: Color,
    val warn: Color,
    val danger: Color,
    val bar: Color,
    val cardBorder: Color,
    val loginBackground: Color,
    val chart: List<Color>,
)

private val LightExtras = CmdbColors(
    ok = SuccessGreen,
    warn = WarningAmber,
    danger = DangerRed,
    bar = BrandNavy,
    cardBorder = Color(0xFFE1E6EE),
    loginBackground = Color.White,
    chart = listOf(BrandNavy, BrandCyan, Color(0xFFC3CBD6), Color(0xFF7E6BE0), SuccessGreen, WarningAmber),
)

private val DarkExtras = CmdbColors(
    ok = Color(0xFF2ECC71),
    warn = Color(0xFFF5A623),
    danger = Color(0xFFE8595E),
    bar = DarkBackground,
    cardBorder = Color(0xFF1E3A52),
    loginBackground = DarkBackground,
    chart = listOf(Color(0xFF1D6FC4), BrandBlue, Color(0xFF8A99AB), Color(0xFF8B7BE8), Color(0xFF2ECC71), Color(0xFFF5A623)),
)

internal val LocalCmdbColors = staticCompositionLocalOf { LightExtras }

private val CmdbLightColors = lightColorScheme(
    primary = BrandNavy,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE3EDF9),
    onPrimaryContainer = BrandNavy,
    secondary = BrandCyan,
    background = LightBackground,
    surface = Color.White,
    surfaceVariant = Color(0xFFEAEFF5),
    onSurface = Color(0xFF15202C),
    onSurfaceVariant = Color(0xFF66748A),
    outline = Color(0xFFC6CFDA),
    error = DangerRed,
)

private val CmdbDarkColors = darkColorScheme(
    primary = BrandBlue,
    onPrimary = Color.White,
    primaryContainer = Color(0xFF10365C),
    onPrimaryContainer = Color(0xFFD9EAFF),
    secondary = BrandCyan,
    background = DarkBackground,
    surface = DarkSurface,
    surfaceVariant = Color(0xFF17304A),
    onSurface = Color(0xFFF2F6FB),
    onSurfaceVariant = Color(0xFF9BAABC),
    outline = Color(0xFF4A5F76),
    error = Color(0xFFE8595E),
)

private fun Context.aktywnosc(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.aktywnosc()
    else -> null
}

@Composable
internal fun CmdbVisualTheme(darkMode: Boolean, content: @Composable () -> Unit) {
    val view = LocalView.current
    if (!view.isInEditMode) SideEffect {
        val window = view.context.aktywnosc()?.window ?: return@SideEffect
        val kontroler = WindowCompat.getInsetsController(window, view)
        // Pasek gorny aplikacji jest ciemny w obu motywach, wiec ikony stanu
        // zawsze jasne. Pasek nawigacji siedzi juz na tle strony.
        kontroler.isAppearanceLightStatusBars = false
        kontroler.isAppearanceLightNavigationBars = !darkMode
    }
    CompositionLocalProvider(
        LocalCmdbColors provides if (darkMode) DarkExtras else LightExtras,
    ) {
        MaterialTheme(
            colorScheme = if (darkMode) CmdbDarkColors else CmdbLightColors,
            typography = MaterialTheme.typography.copy(
                headlineMedium = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                titleLarge = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.SemiBold),
                titleMedium = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
            ),
            shapes = MaterialTheme.shapes.copy(
                small = RoundedCornerShape(8.dp),
                medium = RoundedCornerShape(12.dp),
                large = RoundedCornerShape(16.dp),
            ),
            content = content,
        )
    }
}

@Composable
internal fun ModernLoadingScreen() = Box(
    Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background),
    contentAlignment = Alignment.Center,
) { CircularProgressIndicator(color = MaterialTheme.colorScheme.primary) }

@Composable
internal fun ModernLoginScreen(
    initialServer: String,
    initialEmail: String,
    loading: Boolean,
    error: String?,
    onLogin: (String, String, String) -> Unit,
    onErrorShown: () -> Unit,
    mfaRequired: Boolean = false,
    onCode: (String) -> Unit = {},
    onCancelCode: () -> Unit = {},
    providers: List<pl.hubzso.cmdb.data.ExternalProvider> = emptyList(),
    onLoadProviders: (String) -> Unit = {},
    onExternal: (String, String) -> Unit = { _, _ -> },
) {
    var server by rememberSaveable(initialServer) { mutableStateOf(initialServer) }
    var email by rememberSaveable(initialEmail) { mutableStateOf(initialEmail) }
    var password by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var passwordVisible by remember { mutableStateOf(false) }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(error) { error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(
        containerColor = LocalCmdbColors.current.loginBackground,
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        // Formularz ma byc na srodku ekranu, ale musi dac sie przewinac, gdy
        // klawiatura zabierze pol wysokosci. Samo verticalScroll odbiera
        // kolumnie ograniczenie wysokosci i Arrangement.Center przestaje cokolwiek
        // znaczyc - stad wymuszona wysokosc minimalna rowna widocznemu obszarowi.
        BoxWithConstraints(Modifier.fillMaxSize().padding(padding)) {
            val widok = maxHeight
            Column(
                Modifier
                    .verticalScroll(rememberScrollState())
                    .heightIn(min = widok)
                    .fillMaxWidth()
                    .padding(horizontal = 26.dp, vertical = 24.dp),
                verticalArrangement = Arrangement.Center,
            ) {
                Text(
                    "CMDB",
                    Modifier.fillMaxWidth(),
                    color = MaterialTheme.colorScheme.primary,
                    fontSize = 58.sp,
                    lineHeight = 62.sp,
                    fontWeight = FontWeight.Bold,
                    letterSpacing = 1.sp,
                    textAlign = TextAlign.Center,
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    "Bezpieczny dostęp do infrastruktury",
                    Modifier.fillMaxWidth(),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                )
                Spacer(Modifier.height(44.dp))
                if (mfaRequired) {
                    // Drugi krok: kod z aplikacji uwierzytelniajacej.
                    LoginLabel("Kod z aplikacji uwierzytelniającej")
                    ModernInput(
                        value = code,
                        onValueChange = { code = it.filter { c -> c.isDigit() }.take(6) },
                        placeholder = "6 cyfr",
                        icon = Icons.Outlined.Lock,
                        keyboardType = KeyboardType.NumberPassword,
                    )
                    Spacer(Modifier.height(24.dp))
                    Button(
                        onClick = { onCode(code) },
                        enabled = !loading && code.length == 6,
                        modifier = Modifier.fillMaxWidth().height(54.dp),
                        shape = RoundedCornerShape(10.dp),
                    ) {
                        if (loading) CircularProgressIndicator(Modifier.size(22.dp), color = Color.White, strokeWidth = 2.dp)
                        else Text("Potwierdź", fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                    }
                    TextButton(onClick = { code = ""; onCancelCode() }, modifier = Modifier.fillMaxWidth()) {
                        Text("Wróć")
                    }
                } else {
                LoginLabel("Adres portalu (HTTPS)")
                ModernInput(
                    value = server,
                    onValueChange = { server = it },
                    placeholder = "https://cmdb.twojafirma.pl",
                    icon = Icons.Outlined.Lock,
                    keyboardType = KeyboardType.Uri,
                )
                Spacer(Modifier.height(16.dp))
                LoginLabel("Login")
                ModernInput(
                    value = email,
                    onValueChange = { email = it },
                    placeholder = "jan.kowalski@twojafirma.pl",
                    icon = Icons.Outlined.Email,
                    keyboardType = KeyboardType.Email,
                )
                Spacer(Modifier.height(16.dp))
                LoginLabel("Hasło")
                ModernInput(
                    value = password,
                    onValueChange = { password = it },
                    placeholder = "Wpisz hasło",
                    icon = Icons.Outlined.Lock,
                    visualTransformation = if (passwordVisible) VisualTransformation.None else PasswordVisualTransformation(),
                    trailing = {
                        IconButton(onClick = { passwordVisible = !passwordVisible }) {
                            Icon(
                                if (passwordVisible) Icons.Outlined.VisibilityOff else Icons.Outlined.Visibility,
                                contentDescription = if (passwordVisible) "Ukryj hasło" else "Pokaż hasło",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    },
                )
                Spacer(Modifier.height(30.dp))
                Button(
                    onClick = { onLogin(server, email, password) },
                    enabled = !loading && server.isNotBlank() && email.isNotBlank() && password.isNotBlank(),
                    modifier = Modifier.fillMaxWidth().height(54.dp),
                    shape = RoundedCornerShape(10.dp),
                ) {
                    if (loading) CircularProgressIndicator(Modifier.size(22.dp), color = Color.White, strokeWidth = 2.dp)
                    else Text("Zaloguj", fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                }
                Spacer(Modifier.height(10.dp))
                if (providers.isEmpty()) {
                    TextButton(
                        onClick = { onLoadProviders(server) },
                        enabled = !loading && server.isNotBlank(),
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Konto Google, Microsoft lub GitHub") }
                } else {
                    Text(
                        "Tylko dla osób zaproszonych do CMDB",
                        Modifier.fillMaxWidth(),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        textAlign = TextAlign.Center,
                    )
                    providers.forEach { dostawca ->
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(
                            onClick = { onExternal(server, dostawca.key) },
                            enabled = !loading,
                            modifier = Modifier.fillMaxWidth().height(48.dp),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("Zaloguj przez ${dostawca.name}") }
                    }
                }
                }
            }
        }
    }
}

/** Ekran blokady: aplikacja czeka na odcisk palca. Tresc jest niewidoczna. */
@Composable
internal fun ModernLockScreen(
    email: String,
    reason: String?,
    loading: Boolean,
    error: String?,
    onUnlock: () -> Unit,
    onPassword: () -> Unit,
    onErrorShown: () -> Unit,
) {
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(error) { error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(
        containerColor = LocalCmdbColors.current.loginBackground,
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(horizontal = 26.dp, vertical = 24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("CMDB", color = MaterialTheme.colorScheme.primary, fontSize = 48.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            Text(email, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(40.dp))
            Icon(
                Icons.Outlined.Fingerprint, null, Modifier.size(72.dp),
                MaterialTheme.colorScheme.primary,
            )
            Spacer(Modifier.height(16.dp))
            Text(
                reason ?: "Odblokuj CMDB",
                style = MaterialTheme.typography.titleMedium,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(28.dp))
            Button(
                onClick = onUnlock,
                enabled = !loading,
                modifier = Modifier.fillMaxWidth().height(54.dp),
                shape = RoundedCornerShape(10.dp),
            ) {
                if (loading) CircularProgressIndicator(Modifier.size(22.dp), color = Color.White, strokeWidth = 2.dp)
                else Text("Użyj odcisku palca", fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            }
            TextButton(onClick = onPassword, modifier = Modifier.fillMaxWidth()) { Text("Zaloguj się hasłem") }
        }
    }
}

@Composable private fun LoginLabel(text: String) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Spacer(Modifier.height(7.dp))
}

@Composable private fun ModernInput(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    icon: ImageVector,
    keyboardType: KeyboardType = KeyboardType.Text,
    visualTransformation: VisualTransformation = VisualTransformation.None,
    trailing: @Composable (() -> Unit)? = null,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        modifier = Modifier.fillMaxWidth(),
        placeholder = { Text(placeholder, color = MaterialTheme.colorScheme.onSurfaceVariant) },
        leadingIcon = { Icon(icon, null, Modifier.size(20.dp), MaterialTheme.colorScheme.onSurfaceVariant) },
        trailingIcon = trailing,
        singleLine = true,
        shape = RoundedCornerShape(10.dp),
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        visualTransformation = visualTransformation,
        colors = OutlinedTextFieldDefaults.colors(
            focusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
            unfocusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
            focusedBorderColor = MaterialTheme.colorScheme.primary,
            unfocusedBorderColor = LocalCmdbColors.current.cardBorder,
        ),
    )
}

@Composable
internal fun ModernTenantScreen(state: AppState, onSelect: (pl.hubzso.cmdb.data.Tenant) -> Unit, onLogout: () -> Unit) {
    Scaffold(containerColor = MaterialTheme.colorScheme.background) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item { Text("Wybierz firmę", style = MaterialTheme.typography.headlineMedium) }
            item { Text("Dane aplikacji zostaną ograniczone do wybranej organizacji.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            items(state.tenants, key = { it.id }) { tenant ->
                ElevatedCmdbCard(Modifier.clickable { onSelect(tenant) }) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RoundIcon(Icons.Outlined.Business, MaterialTheme.colorScheme.primary)
                        Spacer(Modifier.width(14.dp))
                        Text(tenant.name, Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                        Icon(Icons.Outlined.ChevronRight, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            if (state.tenants.isEmpty()) item { Text("Brak dostępnych firm") }
            state.error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
            item { TextButton(onClick = onLogout) { Icon(Icons.AutoMirrored.Outlined.Logout, null); Spacer(Modifier.width(6.dp)); Text("Wyloguj") } }
        }
    }
}

// Dolny pasek ma cztery pozycje: trzy stale i helpdesk, ktory pojawia sie
// tylko kontu obslugujacemu zgloszenia. Reszta ekranow wchodzi przez "Więcej" -
// wiecej podpisow nie miesci sie bez scinania.
private enum class ModernTab(val label: String, val icon: ImageVector) {
    DASHBOARD("Pulpit", Icons.Outlined.Home),
    ASSETS("Maszyny", Icons.Outlined.Storage),
    HELPDESK("Helpdesk", Icons.Outlined.SupportAgent),
    MORE("Więcej", Icons.Outlined.MoreHoriz),
}

/**
 * Czynnosci helpdesku podane ekranom jednym pakietem.
 *
 * Zakladka ma ich kilkanascie i wypisanie kazdej osobnym parametrem zamienia
 * naglowek ekranu glownego w liste, w ktorej nie widac juz reszty aplikacji.
 */
internal data class HelpdeskActions(
    val onOpen: (String) -> Unit,
    val onClose: () -> Unit,
    val onSearch: (String, String) -> Unit,
    val onMore: () -> Unit,
    val onRefresh: () -> Unit,
    val onCreate: (NewTicket, List<WybranyPlik>) -> Unit,
    val onSend: (String, NewMessage, List<WybranyPlik>) -> Unit,
    val onStatus: (String, String, String) -> Unit,
    val onAssign: (String, String) -> Unit,
    val onTime: (String, Int, String) -> Unit,
    val onAsset: (String, String, String) -> Unit,
    val onSearchAssets: (String, String) -> Unit,
    val onOpenAttachment: (TicketAttachment) -> Unit,
)

private enum class ModernPage(val label: String, val icon: ImageVector) {
    PEOPLE("Słowniki", Icons.AutoMirrored.Outlined.MenuBook),
    CHANGES("Historia zmian", Icons.Outlined.History),
    REPORTS("Raporty", Icons.Outlined.Assessment),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun ModernMainScreen(
    state: AppState,
    onRefresh: () -> Unit,
    onLogout: () -> Unit,
    onToggleTheme: () -> Unit,
    theme: String,
    onSendReport: (String) -> Unit,
    onSaveReport: (String?, ReportWrite) -> Unit,
    onDeleteReport: (String) -> Unit,
    onOpenAsset: (String) -> Unit,
    onCloseAsset: () -> Unit,
    onSaveDictionary: (String, String?, Map<String, JsonElement>) -> Unit,
    onDeleteDictionary: (String, String) -> Unit,
    onUpdateAssignment: (String, AssignmentWrite) -> Unit,
    onSearchAssets: (String, String, Boolean) -> Unit,
    onMoreAssets: () -> Unit,
    onChooseTenant: () -> Unit,
    onErrorShown: () -> Unit,
    onNoticeShown: () -> Unit,
    helpdesk: HelpdeskActions,
    onToggleBiometrics: () -> Unit = {},
) {
    var tab by rememberSaveable { mutableStateOf(ModernTab.DASHBOARD) }
    var page by rememberSaveable { mutableStateOf<ModernPage?>(null) }
    var helpdeskView by rememberSaveable { mutableStateOf(HelpdeskView.LIST) }
    var menuOpen by remember { mutableStateOf(false) }
    var alertsOpen by remember { mutableStateOf(false) }
    var focusSearch by remember { mutableIntStateOf(0) }
    val snackbar = remember { SnackbarHostState() }
    val kolory = LocalCmdbColors.current
    val detail = state.selectedAsset
    val zgloszenie = state.helpdesk.detail
    // Zakladka helpdesku ma wlasna nawigacje w glab (lista -> karta -> rozmowa),
    // wiec wstecz musi ja odwijac krok po kroku, a nie wychodzic z aplikacji.
    val helpdeskWGlebi = tab == ModernTab.HELPDESK && helpdeskView != HelpdeskView.LIST
    val cofnijHelpdesk = {
        when (helpdeskView) {
            HelpdeskView.THREAD -> helpdeskView = HelpdeskView.TICKET
            HelpdeskView.TICKET -> { helpdeskView = HelpdeskView.LIST; helpdesk.onClose() }
            else -> helpdeskView = HelpdeskView.LIST
        }
    }
    if (detail != null) BackHandler(enabled = !state.saving, onBack = onCloseAsset)
    else if (helpdeskWGlebi) BackHandler(enabled = !state.saving) { cofnijHelpdesk() }
    else if (page != null) BackHandler(enabled = !state.saving) { page = null }
    LaunchedEffect(state.error) { state.error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    LaunchedEffect(state.notice) { state.notice?.let { snackbar.showSnackbar(it); onNoticeShown() } }
    // Konto moze stracic dostep do helpdesku miedzy odswiezeniami (odebrana
    // firma). Zakladka znika z paska, wiec nie wolno na niej zostawic ekranu.
    LaunchedEffect(state.helpdesk.available) {
        if (!state.helpdesk.available && tab == ModernTab.HELPDESK) {
            tab = ModernTab.DASHBOARD
            helpdeskView = HelpdeskView.LIST
        }
    }
    // Zalozone zgloszenie otwiera sie samo: technik wlasnie je opisal i chce
    // zobaczyc numer, a nie wrocic na liste i go szukac.
    LaunchedEffect(zgloszenie?.ticket?.id) {
        if (zgloszenie != null && helpdeskView == HelpdeskView.NEW) helpdeskView = HelpdeskView.TICKET
    }

    val przejdzDoMaszyn = { bezOpiekuna: Boolean ->
        page = null
        tab = ModernTab.ASSETS
        onSearchAssets(state.assetQuery, state.assetOs, bezOpiekuna)
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        detail?.asset?.hostname
                            ?: page?.label
                            ?: when {
                                tab != ModernTab.HELPDESK -> tab.label
                                helpdeskView == HelpdeskView.TICKET -> zgloszenie?.ticket?.number ?: "Zgłoszenie"
                                helpdeskView == HelpdeskView.THREAD -> "Rozmowa"
                                helpdeskView == HelpdeskView.NEW -> "Nowe zgłoszenie"
                                else -> tab.label
                            },
                        fontSize = 24.sp,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                },
                navigationIcon = {
                    if (detail != null || page != null || helpdeskWGlebi) IconButton(
                        enabled = !state.saving,
                        onClick = {
                            when {
                                detail != null -> onCloseAsset()
                                helpdeskWGlebi -> cofnijHelpdesk()
                                else -> page = null
                            }
                        },
                    ) { Icon(Icons.AutoMirrored.Outlined.ArrowBack, "Wstecz") }
                },
                actions = {
                    // Pasek ma akcje tylko dla zakladek. Karta maszyny i ekrany
                    // z "Więcej" maja strzalke wstecz i nic wiecej.
                    val zakladka = if (detail != null || page != null || helpdeskWGlebi) null else tab
                    when (zakladka) {
                        // Dzwonek zbiera to, co na pulpicie jest na czerwono i
                        // bursztynowo. Nie jest ozdoba: prowadzi do listy.
                        ModernTab.DASHBOARD -> Box {
                            IconButton(onClick = { alertsOpen = true }) {
                                Box {
                                    Icon(Icons.Outlined.NotificationsNone, "Powiadomienia")
                                    val alerty = (state.dashboard?.stale ?: 0) + (state.dashboard?.unassigned ?: 0)
                                    if (alerty > 0) Box(
                                        Modifier.size(8.dp).align(Alignment.TopEnd).clip(CircleShape).background(kolory.danger),
                                    )
                                }
                            }
                            DropdownMenu(expanded = alertsOpen, onDismissRequest = { alertsOpen = false }) {
                                val bezKontaktu = state.dashboard?.stale ?: 0
                                val bezOpiekuna = state.dashboard?.unassigned ?: 0
                                if (bezKontaktu == 0 && bezOpiekuna == 0) DropdownMenuItem(
                                    text = { Text("Brak alertów") },
                                    onClick = { alertsOpen = false },
                                    enabled = false,
                                )
                                if (bezKontaktu > 0) DropdownMenuItem(
                                    text = { Text("Bez kontaktu: $bezKontaktu") },
                                    leadingIcon = { Icon(Icons.Outlined.SyncProblem, null, tint = kolory.warn) },
                                    onClick = { alertsOpen = false; przejdzDoMaszyn(state.assetUnassigned) },
                                )
                                if (bezOpiekuna > 0) DropdownMenuItem(
                                    text = { Text("Bez opiekuna: $bezOpiekuna") },
                                    leadingIcon = { Icon(Icons.Outlined.PersonOff, null, tint = kolory.danger) },
                                    onClick = { alertsOpen = false; przejdzDoMaszyn(true) },
                                )
                            }
                        }
                        ModernTab.ASSETS -> {
                            IconButton(onClick = { focusSearch++ }) { Icon(Icons.Outlined.Search, "Szukaj") }
                            Box {
                                IconButton(onClick = { menuOpen = true }) { Icon(Icons.Outlined.MoreVert, "Więcej") }
                                DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                                    DropdownMenuItem(
                                        text = { Text("Odśwież") },
                                        leadingIcon = { Icon(Icons.Outlined.Refresh, null) },
                                        onClick = { menuOpen = false; onRefresh() },
                                    )
                                    if (state.tenants.size > 1) DropdownMenuItem(
                                        text = { Text("Zmień firmę") },
                                        leadingIcon = { Icon(Icons.Outlined.Business, null) },
                                        onClick = { menuOpen = false; onChooseTenant() },
                                    )
                                    DropdownMenuItem(
                                        text = { Text("Motyw: ${opisMotywu(theme)}") },
                                        leadingIcon = { Icon(if (theme == "dark") Icons.Outlined.DarkMode else Icons.Outlined.LightMode, null) },
                                        onClick = { menuOpen = false; onToggleTheme() },
                                    )
                                }
                            }
                        }
                        ModernTab.HELPDESK -> {
                            IconButton(onClick = { focusSearch++ }) { Icon(Icons.Outlined.Search, "Szukaj") }
                            IconButton(onClick = helpdesk.onRefresh) { Icon(Icons.Outlined.Refresh, "Odśwież") }
                        }
                        ModernTab.MORE, null -> Unit
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = kolory.bar,
                    titleContentColor = Color.White,
                    navigationIconContentColor = Color.White,
                    actionIconContentColor = Color.White,
                ),
            )
        },
        bottomBar = {
            // Karta maszyny i wnetrze helpdesku (zgloszenie, rozmowa, formularz)
            // zajmuja caly ekran - pasek wrocilby tam tylko po to, zeby zabrac
            // miejsce polu wiadomosci.
            if (detail == null && !helpdeskWGlebi) Column {
                HorizontalDivider(color = LocalCmdbColors.current.cardBorder)
                NavigationBar(
                    containerColor = MaterialTheme.colorScheme.surface,
                    tonalElevation = 0.dp,
                ) {
                    val czeka = state.helpdesk.catalog?.waiting ?: state.helpdesk.counters.waiting
                    ModernTab.entries.filter {
                        // Zgloszenia sa osobnym uprawnieniem - konto bez dostepu
                        // nie dostaje zakladki, ktora i tak odpowie odmowa.
                        it != ModernTab.HELPDESK || state.helpdesk.available
                    }.forEach { item ->
                        NavigationBarItem(
                            selected = tab == item && page == null,
                            enabled = !state.saving,
                            onClick = { tab = item; page = null },
                            icon = {
                                Box {
                                    Icon(item.icon, item.label, Modifier.size(24.dp))
                                    if (item == ModernTab.HELPDESK && czeka > 0) Box(
                                        Modifier.align(Alignment.TopEnd)
                                            .offset(x = 9.dp, y = (-5).dp)
                                            .clip(CircleShape)
                                            .background(kolory.danger)
                                            .padding(horizontal = 4.dp, vertical = 1.dp),
                                    ) {
                                        Text(
                                            if (czeka > 99) "99+" else "$czeka",
                                            color = Color.White,
                                            fontSize = 9.sp,
                                            fontWeight = FontWeight.Bold,
                                        )
                                    }
                                }
                            },
                            label = { Text(item.label, fontSize = 11.sp, maxLines = 1) },
                            colors = NavigationBarItemDefaults.colors(
                                selectedIconColor = MaterialTheme.colorScheme.primary,
                                selectedTextColor = MaterialTheme.colorScheme.primary,
                                unselectedIconColor = MaterialTheme.colorScheme.onSurfaceVariant,
                                unselectedTextColor = MaterialTheme.colorScheme.onSurfaceVariant,
                                indicatorColor = Color.Transparent,
                            ),
                        )
                    }
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        if (state.loading && state.dashboard == null) ModernLoadingScreen()
        else if (detail != null) ModernAssetDetailScreen(
            detail, state.dictionaries, state.user?.canWrite == true,
            padding, state.saving, state.mutationVersion, onUpdateAssignment,
        )
        else when (page) {
            ModernPage.PEOPLE -> VisualDictionariesScreen(
                state.dictionaryCategories, state.dictionarySchemas, state.dictionaries,
                state.user?.canWrite == true, padding, state.saving, state.mutationVersion,
                state.error, onSaveDictionary, onDeleteDictionary,
            )
            ModernPage.CHANGES -> ModernChangesScreen(state.changes, padding)
            ModernPage.REPORTS -> VisualReportManagementScreen(
                state.reports, state.reportCatalog, padding, state.user?.canWrite == true,
                state.saving, state.mutationVersion, state.error, onSendReport, onSaveReport, onDeleteReport,
            )
            null -> when (tab) {
                ModernTab.DASHBOARD -> ModernDashboardScreen(state.dashboard, padding, onOpenAsset)
                ModernTab.ASSETS -> ModernAssetList(state, padding, focusSearch, onOpenAsset, onSearchAssets, onMoreAssets)
                ModernTab.HELPDESK -> when (helpdeskView) {
                    HelpdeskView.LIST -> HelpdeskListScreen(
                        state.helpdesk, padding, focusSearch,
                        onOpen = { helpdeskView = HelpdeskView.TICKET; helpdesk.onOpen(it) },
                        onSearch = helpdesk.onSearch,
                        onMore = helpdesk.onMore,
                        onNew = { helpdesk.onClose(); helpdeskView = HelpdeskView.NEW },
                    )
                    HelpdeskView.NEW -> HelpdeskNewTicketScreen(
                        state.helpdesk.catalog, state.helpdesk.assetPicker, padding, state.saving,
                        onSearchAssets = helpdesk.onSearchAssets,
                        onCreate = helpdesk.onCreate,
                    )
                    HelpdeskView.TICKET, HelpdeskView.THREAD ->
                        if (zgloszenie == null) ModernLoadingScreen()
                        else if (helpdeskView == HelpdeskView.TICKET) HelpdeskTicketScreen(
                            zgloszenie,
                            state.helpdesk.catalog?.statuses.orEmpty(),
                            state.helpdesk.assetPicker,
                            padding, state.saving, state.mutationVersion,
                            onThread = { helpdeskView = HelpdeskView.THREAD },
                            onStatus = { status, podsumowanie ->
                                helpdesk.onStatus(zgloszenie.ticket.id, status, podsumowanie)
                            },
                            onAssign = { helpdesk.onAssign(zgloszenie.ticket.id, it) },
                            onTime = { minuty, opis -> helpdesk.onTime(zgloszenie.ticket.id, minuty, opis) },
                            onAsset = { asset, akcja -> helpdesk.onAsset(zgloszenie.ticket.id, asset, akcja) },
                            onSearchAssets = { helpdesk.onSearchAssets(zgloszenie.ticket.tenantId, it) },
                            onOpenAttachment = helpdesk.onOpenAttachment,
                        )
                        else HelpdeskThreadScreen(
                            zgloszenie, padding, state.saving, state.mutationVersion,
                            onSend = { wiadomosc, pliki ->
                                helpdesk.onSend(zgloszenie.ticket.id, wiadomosc, pliki)
                            },
                            onOpenAttachment = helpdesk.onOpenAttachment,
                        )
                }
                ModernTab.MORE -> ModernMoreScreen(
                    state, theme, padding,
                    onOpenPage = { page = it },
                    onRefresh = onRefresh,
                    onToggleTheme = onToggleTheme,
                    onChooseTenant = onChooseTenant,
                    onLogout = onLogout,
                    onToggleBiometrics = onToggleBiometrics,
                )
            }
        }
    }
}

private fun opisMotywu(theme: String) = when (theme) {
    "dark" -> "ciemny"
    "light" -> "jasny"
    else -> "systemowy"
}

@Composable private fun ModernDashboardScreen(data: Dashboard?, padding: PaddingValues, onOpen: (String) -> Unit) {
    val kolory = LocalCmdbColors.current
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                KpiCard("Maszyny", data?.total ?: 0, Icons.Outlined.Storage, MaterialTheme.colorScheme.primary, Modifier.weight(1f))
                KpiCard("Bez kontaktu", data?.stale ?: 0, Icons.Outlined.SyncProblem, kolory.warn, Modifier.weight(1f))
                KpiCard("Bez opiekuna", data?.unassigned ?: 0, Icons.Outlined.PersonOff, kolory.danger, Modifier.weight(1f))
            }
        }
        val systemy = data?.byOs.orEmpty()
        if (systemy.isNotEmpty()) item {
            ElevatedCmdbCard {
                Text("Dystrybucja systemów operacyjnych", style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(12.dp))
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    OsDonut(systemy, Modifier.size(146.dp))
                    Spacer(Modifier.width(14.dp))
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        val suma = systemy.sumOf { it.count }.coerceAtLeast(1)
                        systemy.take(4).forEachIndexed { index, item ->
                            LegendRow(item, kolory.chart[index % kolory.chart.size], suma)
                        }
                    }
                }
            }
        }
        item {
            ElevatedCmdbCard(padding = 0.dp) {
                Text(
                    "Ostatni kontakt",
                    Modifier.padding(start = 15.dp, end = 15.dp, top = 15.dp, bottom = 4.dp),
                    style = MaterialTheme.typography.titleMedium,
                )
                val recent = data?.recent.orEmpty()
                if (recent.isEmpty()) Text(
                    "Brak ostatnio widzianych urządzeń",
                    Modifier.padding(15.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                recent.forEachIndexed { index, asset ->
                    if (index > 0) HorizontalDivider(color = kolory.cardBorder)
                    RecentRow(asset) { onOpen(asset.id) }
                }
            }
        }
    }
}

@Composable private fun KpiCard(label: String, value: Int, icon: ImageVector, color: Color, modifier: Modifier) {
    val kolory = LocalCmdbColors.current
    Card(
        modifier,
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, kolory.cardBorder),
    ) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 14.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Icon(icon, null, tint = color, modifier = Modifier.size(26.dp))
            Spacer(Modifier.height(9.dp))
            Text(
                label,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
            )
            Spacer(Modifier.height(3.dp))
            Text(value.toString(), fontSize = 26.sp, fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
        }
    }
}

/**
 * Pierscien z udzialem procentowym wpisanym w wycinek. Podpisy ponizej progu
 * pomijamy - na 40 dp luku i tak by sie nie zmiescily.
 */
@Composable private fun OsDonut(items: List<CountItem>, modifier: Modifier) {
    val total = items.sumOf { it.count }.coerceAtLeast(1)
    val chart = LocalCmdbColors.current.chart
    val miara = rememberTextMeasurer()
    val widoczne = items.take(chart.size)
    Canvas(modifier) {
        val grubosc = size.minDimension * 0.27f
        val bok = size.minDimension - grubosc
        val lewy = Offset((size.width - bok) / 2f, (size.height - bok) / 2f)
        val promien = bok / 2f
        val srodek = Offset(size.width / 2f, size.height / 2f)
        var kat = -90f
        // Wlosowa przerwa miedzy wycinkami. Przy jednym systemie odpuszczamy,
        // zeby pelny pierscien nie mial wyszczerbienia bez powodu.
        val przerwa = if (widoczne.size > 1) 1.5f else 0f
        widoczne.forEachIndexed { index, item ->
            val udzial = item.count.toFloat() / total
            val wycinek = 360f * udzial
            val color = chart[index % chart.size]
            drawArc(
                color = color,
                startAngle = kat,
                sweepAngle = (wycinek - przerwa).coerceAtLeast(0.8f),
                useCenter = false,
                topLeft = lewy,
                size = Size(bok, bok),
                style = Stroke(grubosc),
            )
            if (udzial >= 0.07f) {
                val srodkowy = Math.toRadians((kat + wycinek / 2f).toDouble())
                val podpis = "${Math.round(udzial * 100)}%"
                val uklad = miara.measure(
                    AnnotatedString(podpis),
                    TextStyle(
                        color = if (color.luminance() > 0.5f) Color(0xFF15202C) else Color.White,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold,
                    ),
                )
                drawText(
                    uklad,
                    topLeft = Offset(
                        srodek.x + (promien * cos(srodkowy)).toFloat() - uklad.size.width / 2f,
                        srodek.y + (promien * sin(srodkowy)).toFloat() - uklad.size.height / 2f,
                    ),
                )
            }
            kat += wycinek
        }
    }
}

@Composable private fun LegendRow(item: CountItem, color: Color, total: Int) {
    Row(verticalAlignment = Alignment.Top) {
        Box(Modifier.padding(top = 5.dp).size(10.dp).clip(CircleShape).background(color))
        Spacer(Modifier.width(9.dp))
        Column {
            Text(item.label, style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                "${Math.round(item.count * 100f / total)}% (${item.count})",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** Wiersz na pulpicie: bez opiekuna i bez strzalki, bo pulpit ma byc skrotem. */
@Composable private fun RecentRow(asset: AssetSummary, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 15.dp, vertical = 11.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Image(painterResource(osIkona(asset.osFamily, asset.type)), null, Modifier.size(24.dp))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(asset.hostname, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(asset.primaryIp ?: "Brak adresu IP", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.width(8.dp))
        Text(wzglednyCzas(asset.lastSeen), fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
        Spacer(Modifier.width(9.dp))
        Box(Modifier.size(9.dp).clip(CircleShape).background(kolorStanu(asset)))
    }
}

@Composable private fun ModernAssetList(
    state: AppState,
    padding: PaddingValues,
    focusSearch: Int,
    onOpen: (String) -> Unit,
    onSearch: (String, String, Boolean) -> Unit,
    onMore: () -> Unit,
) {
    val kolory = LocalCmdbColors.current
    val lista = rememberLazyListState()
    val fokus = remember { FocusRequester() }
    // Lupa w pasku gornym nie otwiera osobnego ekranu - pole jest juz na liscie,
    // wiec przewijamy je na wierzch i ustawiamy kursor. Licznik zapamietany przy
    // wejsciu na zakladke sprawia, ze samo wrocenie na nia nie wyrzuca klawiatury.
    var obsluzone by remember { mutableIntStateOf(focusSearch) }
    LaunchedEffect(focusSearch) {
        if (focusSearch != obsluzone) {
            obsluzone = focusSearch
            lista.scrollToItem(0)
            runCatching { fokus.requestFocus() }
        }
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        state = lista,
        contentPadding = PaddingValues(start = 14.dp, end = 14.dp, top = 12.dp, bottom = 14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            OutlinedTextField(
                value = state.assetQuery,
                onValueChange = { onSearch(it, state.assetOs, state.assetUnassigned) },
                modifier = Modifier.fillMaxWidth().focusRequester(fokus),
                placeholder = { Text("Szukaj maszyny, IP lub numeru seryjnego", fontSize = 14.sp) },
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
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                item { CmdbFilterChip("Wszystkie", state.assetOs.isEmpty() && !state.assetUnassigned) { onSearch(state.assetQuery, "", false) } }
                items(state.dashboard?.byOs.orEmpty().filter { !it.key.isNullOrBlank() }) { os ->
                    CmdbFilterChip(os.label, state.assetOs == os.key) { onSearch(state.assetQuery, os.key.orEmpty(), state.assetUnassigned) }
                }
                item { CmdbFilterChip("Bez opiekuna", state.assetUnassigned) { onSearch(state.assetQuery, state.assetOs, !state.assetUnassigned) } }
            }
        }
        items(state.assets, key = { it.id }) { asset -> ModernAssetCard(asset) { onOpen(asset.id) } }
        if (state.assetLoading) item { Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() } }
        else if (state.assets.isEmpty()) item { EmptyState("Brak maszyn spełniających kryteria") }
        if (state.assets.size < state.assetTotal) item {
            OutlinedButton(onClick = onMore, enabled = !state.assetLoading, modifier = Modifier.fillMaxWidth()) {
                Text("Załaduj kolejne (${state.assets.size} z ${state.assetTotal})")
            }
        }
    }
}

@Composable internal fun CmdbFilterChip(label: String, selected: Boolean, onClick: () -> Unit) {
    FilterChip(
        selected = selected,
        onClick = onClick,
        label = { Text(label, fontSize = 13.sp) },
        shape = RoundedCornerShape(18.dp),
        border = if (selected) null else BorderStroke(1.dp, LocalCmdbColors.current.cardBorder),
        colors = FilterChipDefaults.filterChipColors(
            containerColor = MaterialTheme.colorScheme.surface,
            labelColor = MaterialTheme.colorScheme.onSurface,
            selectedContainerColor = MaterialTheme.colorScheme.primary,
            selectedLabelColor = Color.White,
        ),
    )
}

@Composable private fun ModernAssetCard(asset: AssetSummary, onClick: () -> Unit) {
    val kolory = LocalCmdbColors.current
    Card(
        Modifier.fillMaxWidth().clickable(onClick = onClick),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, kolory.cardBorder),
    ) {
        Row(
            Modifier.fillMaxWidth().height(IntrinsicSize.Min).padding(14.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Image(painterResource(osIkona(asset.osFamily, asset.type)), null, Modifier.padding(top = 2.dp).size(34.dp))
            Spacer(Modifier.width(13.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(asset.hostname, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    listOfNotNull(asset.primaryIp, asset.osFamily).joinToString(" • ").ifBlank { "Brak danych sieciowych" },
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    "Opiekun: ${asset.owner?.value ?: "brak"}",
                    fontSize = 12.sp,
                    color = if (asset.owner == null) kolory.danger else MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Row {
                    Text("Ostatni kontakt: ", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(wzglednyCzas(asset.lastSeen), fontSize = 12.sp, color = kolorStanu(asset), fontWeight = FontWeight.Medium)
                }
            }
            Spacer(Modifier.width(6.dp))
            Box(Modifier.fillMaxHeight().width(22.dp)) {
                Box(Modifier.align(Alignment.TopEnd).size(9.dp).clip(CircleShape).background(kolorStanu(asset)))
                Icon(
                    Icons.Outlined.ChevronRight,
                    null,
                    Modifier.align(Alignment.CenterEnd).size(22.dp),
                    MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable private fun ModernMoreScreen(
    state: AppState,
    theme: String,
    padding: PaddingValues,
    onOpenPage: (ModernPage) -> Unit,
    onRefresh: () -> Unit,
    onToggleTheme: () -> Unit,
    onChooseTenant: () -> Unit,
    onLogout: () -> Unit,
    onToggleBiometrics: () -> Unit = {},
) {
    val kolory = LocalCmdbColors.current
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            ElevatedCmdbCard {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RoundIcon(Icons.Outlined.Person, MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(13.dp))
                    Column(Modifier.weight(1f)) {
                        Text(state.user?.fullName ?: state.user?.email.orEmpty(), style = MaterialTheme.typography.titleMedium)
                        Text(
                            listOfNotNull(state.user?.tenant?.name, state.user?.role).joinToString(" • "),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
        item {
            ElevatedCmdbCard(padding = 0.dp) {
                ModernPage.entries.forEachIndexed { index, entry ->
                    if (index > 0) HorizontalDivider(color = kolory.cardBorder)
                    MoreRow(entry.icon, entry.label, null) { onOpenPage(entry) }
                }
            }
        }
        item {
            ElevatedCmdbCard(padding = 0.dp) {
                MoreRow(Icons.Outlined.Refresh, "Odśwież dane", null, onClick = onRefresh)
                HorizontalDivider(color = kolory.cardBorder)
                MoreRow(
                    if (theme == "dark") Icons.Outlined.DarkMode else Icons.Outlined.LightMode,
                    "Motyw",
                    opisMotywu(theme),
                    onClick = onToggleTheme,
                )
                if (state.tenants.size > 1) {
                    HorizontalDivider(color = kolory.cardBorder)
                    MoreRow(Icons.Outlined.Business, "Zmień firmę", state.user?.tenant?.name, onClick = onChooseTenant)
                }
                HorizontalDivider(color = kolory.cardBorder)
                MoreRow(
                    Icons.Outlined.Fingerprint,
                    "Logowanie odciskiem palca",
                    if (state.biometricsEnabled) "włączone" else "wyłączone",
                    onClick = onToggleBiometrics,
                )
                HorizontalDivider(color = kolory.cardBorder)
                MoreRow(Icons.AutoMirrored.Outlined.Logout, "Wyloguj", null, barwa = kolory.danger, onClick = onLogout)
            }
        }
    }
}

@Composable private fun MoreRow(
    icon: ImageVector,
    label: String,
    value: String?,
    barwa: Color? = null,
    onClick: () -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 15.dp, vertical = 15.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, null, Modifier.size(22.dp), barwa ?: MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(14.dp))
        Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge, color = barwa ?: MaterialTheme.colorScheme.onSurface)
        if (value != null) Text(value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(6.dp))
        Icon(Icons.Outlined.ChevronRight, null, Modifier.size(20.dp), MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/**
 * Kropka i kolor czasu mowia o swiezosci danych, a nie o polityce firmy.
 * Prog "bez kontaktu" liczy serwer wedlug ustawien najemcy i pokazujemy go
 * osobno na kafelku - te dwie liczby nie musza sie zgadzac i nie udajemy, ze sa
 * tym samym.
 */
@Composable private fun kolorStanu(asset: AssetSummary): Color {
    val kolory = LocalCmdbColors.current
    if (asset.lifecycle.contains("wycof", true) || asset.lifecycle.contains("retir", true)) return kolory.danger
    val minuty = minutOd(asset.lastSeen) ?: return kolory.danger
    return when {
        minuty < 60 -> kolory.ok
        minuty < 24 * 60 -> kolory.warn
        else -> kolory.danger
    }
}

/**
 * Ikona po rodzinie systemu, a w razie potrzeby po typie zasobu. Kolejnosc jest
 * istotna: "Cisco IOS 15.2" to przelacznik, a nie iPhone, wiec sprzet sieciowy
 * musi byc sprawdzony przed Apple. Z tego samego powodu nie szukamy golego
 * "mac" ani "ios" - za latwo trafiaja w cudze nazwy.
 */
private fun osIkona(os: String?, typ: String?): Int {
    val tekst = "${os.orEmpty()} ${typ.orEmpty()}".lowercase()
    return when {
        tekst.contains("windows") -> R.drawable.ic_os_windows
        tekst.contains("raspberry") || tekst.contains("raspbian") -> R.drawable.ic_os_raspberry
        tekst.contains("cisco") || tekst.contains("junos") || tekst.contains("mikrotik") ||
            tekst.contains("routeros") || tekst.contains("switch") || tekst.contains("router") ||
            tekst.contains("przelacznik") || tekst.contains("przełącznik") -> R.drawable.ic_os_switch
        tekst.contains("macos") || tekst.contains("mac os") || tekst.contains("os x") ||
            tekst.contains("osx") || tekst.contains("darwin") -> R.drawable.ic_os_apple
        tekst.contains("linux") || tekst.contains("ubuntu") || tekst.contains("debian") ||
            tekst.contains("centos") || tekst.contains("fedora") || tekst.contains("rhel") ||
            tekst.contains("suse") || tekst.contains("alpine") -> R.drawable.ic_os_linux
        else -> R.drawable.ic_os_server
    }
}

private fun osColor(os: String?): Color = when {
    os?.contains("windows", true) == true -> BrandBlue
    os?.contains("linux", true) == true -> Color(0xFFF0A020)
    os?.contains("mac", true) == true -> Color(0xFF8793A1)
    else -> BrandCyan
}

@Composable private fun ModernAssetDetailScreen(
    data: AssetDetail,
    dictionaries: Map<String, List<DictionaryEntry>>,
    canWrite: Boolean,
    padding: PaddingValues,
    saving: Boolean,
    mutationVersion: Int,
    onSaveAssignment: (String, AssignmentWrite) -> Unit,
) {
    var editAssignment by remember(data.asset.id) { mutableStateOf(false) }
    var tab by rememberSaveable(data.asset.id) { mutableIntStateOf(0) }
    val kolory = LocalCmdbColors.current
    LaunchedEffect(mutationVersion) { editAssignment = false }
    if (editAssignment) {
        ModernAssignmentEditor(data.asset, dictionaries, padding, saving, { editAssignment = false }) {
            onSaveAssignment(data.asset.id, it)
        }
        return
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(0.dp),
    ) {
        item {
            Column(Modifier.fillMaxWidth().background(kolory.bar).padding(horizontal = 16.dp, vertical = 10.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StatusPill(wzglednyCzas(data.asset.lastSeen), kolorStanu(data.asset))
                    Spacer(Modifier.weight(1f))
                    if (canWrite) TextButton(onClick = { editAssignment = true }) { Text("EDYTUJ", color = Color.White, fontWeight = FontWeight.Bold) }
                }
            }
        }
        item {
            ElevatedCmdbCard(Modifier.padding(14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Image(painterResource(osIkona(data.asset.osFamily, data.asset.type)), null, Modifier.size(58.dp))
                    Spacer(Modifier.width(16.dp))
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(data.asset.hostname, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                        Text(data.asset.osFamily ?: "Nieznany system", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(data.asset.primaryIp ?: "Brak adresu IP", fontWeight = FontWeight.Medium)
                    }
                }
            }
        }
        item {
            val tabs = listOf("Podsumowanie", "Sprzęt", "System", "Sieć")
            TabRow(selectedTabIndex = tab, containerColor = MaterialTheme.colorScheme.surface) {
                tabs.forEachIndexed { index, title -> Tab(selected = tab == index, onClick = { tab = index }, text = { Text(title, maxLines = 1, fontSize = 11.sp) }) }
            }
        }
        when (tab) {
            0 -> item { AssetOverview(data) }
            1 -> item { JsonSection("Sprzęt", data.facts) }
            2 -> item { JsonSection("System i dane agenta", data.currentReport.orEmpty()) }
            else -> item { JsonSection("Sieć i atrybuty", data.attributes) }
        }
    }
}

@Composable private fun AssetOverview(data: AssetDetail) {
    val kolory = LocalCmdbColors.current
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            DetailMetric("System", data.asset.osFamily ?: "—", Icons.Outlined.Dns, osColor(data.asset.osFamily), Modifier.weight(1f))
            DetailMetric("Typ", data.asset.type, Icons.Outlined.Computer, MaterialTheme.colorScheme.primary, Modifier.weight(1f))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            DetailMetric("Opiekun", data.asset.owner?.value ?: "Brak", Icons.Outlined.Person, if (data.asset.owner == null) kolory.danger else kolory.ok, Modifier.weight(1f))
            DetailMetric("Lokalizacja", data.asset.location?.value ?: "Brak", Icons.Outlined.LocationOn, kolory.warn, Modifier.weight(1f))
        }
        SectionHeading("Informacje")
        ElevatedCmdbCard {
            InfoLine("Użytkownik", data.asset.user?.value)
            InfoLine("Rola", data.asset.roleLabel)
            InfoLine("Miejsce", data.asset.place)
            InfoLine("Źródło", data.asset.source)
            InfoLine("Ostatni kontakt", wzglednyCzas(data.asset.lastSeen))
        }
    }
}

@Composable private fun DetailMetric(label: String, value: String, icon: ImageVector, color: Color, modifier: Modifier) {
    ElevatedCmdbCard(modifier) {
        Icon(icon, null, tint = color)
        Spacer(Modifier.height(10.dp))
        Text(value, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun JsonSection(title: String, values: Map<String, JsonElement>) {
    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading(title)
        if (values.isEmpty()) EmptyState("Brak danych w tej sekcji")
        values.forEach { (key, value) ->
            ElevatedCmdbCard { Text(humanize(key), style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary); Spacer(Modifier.height(5.dp)); Text(jsonDisplay(value), color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
}

private fun JsonObject?.orEmpty(): Map<String, JsonElement> = this ?: emptyMap()
private fun humanize(value: String) = value.replace('_', ' ').replaceFirstChar { it.uppercase() }
private fun jsonDisplay(value: JsonElement): String = when (value) {
    JsonNull -> "—"
    is JsonPrimitive -> value.contentOrNull ?: value.toString()
    is JsonObject -> value.entries.take(5).joinToString("\n") { "${humanize(it.key)}: ${jsonDisplay(it.value)}" }
    else -> value.toString().take(600)
}

@Composable private fun ModernAssignmentEditor(
    asset: AssetSummary,
    dictionaries: Map<String, List<DictionaryEntry>>,
    padding: PaddingValues,
    saving: Boolean,
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
    BackHandler(enabled = !saving, onBack = onDismiss)
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { Text("Przypisania", style = MaterialTheme.typography.headlineMedium); Text(asset.hostname, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        item { ModernChoiceField("Opiekun", owner, people) { owner = it } }
        item { ModernChoiceField("Użytkownik", user, people) { user = it } }
        item { ModernChoiceField("Lokalizacja", location, locations) { location = it } }
        item { OutlinedTextField(role, { role = it }, label = { Text("Rola") }, leadingIcon = { Icon(Icons.Outlined.Badge, null) }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) }
        item { OutlinedTextField(place, { place = it }, label = { Text("Miejsce") }, leadingIcon = { Icon(Icons.Outlined.LocationOn, null) }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(onClick = onDismiss, enabled = !saving, modifier = Modifier.weight(1f)) { Text("ANULUJ") }
                Button(
                    onClick = { onSave(AssignmentWrite(owner.ifBlank { null }, user.ifBlank { null }, location.ifBlank { null }, role.ifBlank { null }, place.ifBlank { null })) },
                    enabled = !saving,
                    modifier = Modifier.weight(1f),
                ) { Icon(Icons.Outlined.Save, null); Spacer(Modifier.width(6.dp)); Text("ZAPISZ") }
            }
        }
    }
}

@Composable internal fun ModernChoiceField(label: String, value: String, options: List<Pair<String, String>>, onChange: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val shown = options.firstOrNull { it.first == value }?.second ?: "Wybierz"
    Box(Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth().height(56.dp), shape = RoundedCornerShape(12.dp)) {
            Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.weight(1f))
            Text(shown, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Icon(Icons.Outlined.KeyboardArrowDown, null)
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(text = { Text("— brak —") }, onClick = { onChange(""); expanded = false })
            options.forEach { option -> DropdownMenuItem(text = { Text(option.second) }, onClick = { onChange(option.first); expanded = false }) }
        }
    }
}

@Composable private fun ModernChangesScreen(data: List<ChangeEntry>, padding: PaddingValues) {
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(0.dp),
    ) {
        itemsIndexed(data, key = { _, item -> item.id }) { index, change -> TimelineChange(change, index != data.lastIndex) }
        if (data.isEmpty()) item { EmptyState("Brak zarejestrowanych zmian") }
    }
}

@Composable private fun TimelineChange(change: ChangeEntry, showLine: Boolean) {
    val kolory = LocalCmdbColors.current
    val color = when (change.action.lowercase()) {
        "created", "utworzono", "added" -> kolory.ok
        "deleted", "usunięto" -> kolory.danger
        else -> MaterialTheme.colorScheme.primary
    }
    Row(Modifier.fillMaxWidth()) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Box(Modifier.size(34.dp).clip(CircleShape).background(color.copy(alpha = .14f)), contentAlignment = Alignment.Center) {
                Icon(Icons.Outlined.History, null, tint = color, modifier = Modifier.size(18.dp))
            }
            if (showLine) Canvas(Modifier.width(2.dp).height(94.dp)) { drawLine(color.copy(alpha = .28f), Offset(size.width / 2, 0f), Offset(size.width / 2, size.height), strokeWidth = size.width, cap = StrokeCap.Round) }
        }
        Spacer(Modifier.width(12.dp))
        ElevatedCmdbCard(Modifier.weight(1f).padding(bottom = 12.dp)) {
            Text(change.hostname, style = MaterialTheme.typography.titleMedium)
            Text("${change.action}: ${change.label}", color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (!change.oldValue.isNullOrBlank() || !change.newValue.isNullOrBlank()) {
                Spacer(Modifier.height(8.dp))
                Text("${change.oldValue ?: "—"}  →  ${change.newValue ?: "—"}", color = color, fontWeight = FontWeight.Medium)
            }
            Spacer(Modifier.height(6.dp))
            Text(wzglednyCzas(change.occurredAt), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable internal fun ElevatedCmdbCard(
    modifier: Modifier = Modifier,
    padding: androidx.compose.ui.unit.Dp = 15.dp,
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, LocalCmdbColors.current.cardBorder),
    ) { Column(Modifier.fillMaxWidth().padding(padding), content = content) }
}

@Composable internal fun RoundIcon(icon: ImageVector, color: Color) {
    Box(Modifier.size(42.dp).clip(CircleShape).background(color.copy(alpha = .14f)), contentAlignment = Alignment.Center) {
        Icon(icon, null, tint = color, modifier = Modifier.size(22.dp))
    }
}

@Composable internal fun SectionHeading(text: String) = Text(text, style = MaterialTheme.typography.titleLarge)

@Composable internal fun StatusPill(text: String, color: Color) {
    Row(Modifier.clip(RoundedCornerShape(20.dp)).background(color.copy(alpha = .20f)).padding(horizontal = 10.dp, vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(7.dp).clip(CircleShape).background(color)); Spacer(Modifier.width(6.dp)); Text(text, color = color, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
    }
}

@Composable internal fun InfoLine(label: String, value: String?) {
    if (!value.isNullOrBlank()) Row(Modifier.fillMaxWidth().padding(vertical = 7.dp), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, Modifier.weight(1f), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, Modifier.weight(1.2f), fontWeight = FontWeight.Medium)
    }
}

@Composable internal fun EmptyState(text: String) {
    ElevatedCmdbCard { Text(text, Modifier.align(Alignment.CenterHorizontally), color = MaterialTheme.colorScheme.onSurfaceVariant) }
}
