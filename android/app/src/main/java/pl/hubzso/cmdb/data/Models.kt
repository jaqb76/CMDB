package pl.hubzso.cmdb.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable data class LoginRequest(
    val email: String,
    val password: String,
    // Kod z aplikacji uwierzytelniajacej - tylko dla kont z weryfikacja dwuetapowa.
    val code: String? = null,
)
@Serializable data class LoginResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("expires_in") val expiresIn: Long,
    val user: User,
    val policy: MobilePolicy = MobilePolicy(),
)

/** Zasady firmy dla aplikacji: biometria, pelne logowanie co N dni, blokada. */
@Serializable data class MobilePolicy(
    val biometrics: String = "dozwolona",
    @SerialName("full_login_days") val fullLoginDays: Int = 30,
    @SerialName("allow_device_credential") val allowDeviceCredential: Boolean = false,
    @SerialName("lock_after_minutes") val lockAfterMinutes: Int = 5,
)

@Serializable data class DeviceRequest(
    val name: String,
    @SerialName("public_key") val publicKey: String,
    val replaces: String? = null,
)
@Serializable data class Device(val id: String, val name: String)
@Serializable data class DeviceResponse(val device: Device, val policy: MobilePolicy = MobilePolicy())
@Serializable data class ChallengeRequest(@SerialName("device_id") val deviceId: String)
@Serializable data class ChallengeResponse(val challenge: String)
@Serializable data class BiometricRequest(
    @SerialName("device_id") val deviceId: String,
    val challenge: String,
    val signature: String,
)
@Serializable data class CodeLoginRequest(val code: String, val verifier: String)
@Serializable data class ExternalProvider(val key: String, val name: String)
@Serializable data class LoginMethods(
    val sposob: String? = null,
    val firma: String? = null,
    val external: List<ExternalProvider> = emptyList(),
)

@Serializable data class User(
    val id: String,
    val email: String,
    @SerialName("full_name") val fullName: String? = null,
    val role: String,
    @SerialName("can_write") val canWrite: Boolean,
    val tenant: Tenant? = null,
)

@Serializable data class Tenant(val id: String, val name: String, val slug: String)

@Serializable data class Dashboard(
    val total: Int,
    val stale: Int,
    val unassigned: Int,
    @SerialName("with_agent") val withAgent: Int,
    @SerialName("by_os") val byOs: List<CountItem> = emptyList(),
    @SerialName("by_type") val byType: List<CountItem> = emptyList(),
    val recent: List<AssetSummary> = emptyList(),
    val changed: List<AssetSummary> = emptyList(),
)

@Serializable data class CountItem(val key: String? = null, val label: String, val count: Int)

@Serializable data class AssetSummary(
    val id: String,
    val hostname: String,
    val type: String,
    val source: String,
    @SerialName("os_family") val osFamily: String? = null,
    @SerialName("primary_ip") val primaryIp: String? = null,
    @SerialName("last_seen") val lastSeen: String? = null,
    val lifecycle: String,
    val owner: DictionaryEntry? = null,
    val user: DictionaryEntry? = null,
    val location: DictionaryEntry? = null,
    @SerialName("role_label") val roleLabel: String? = null,
    val place: String? = null,
)

@Serializable data class AssetPage(
    val items: List<AssetSummary>,
    val page: Int,
    @SerialName("page_size") val pageSize: Int,
    val total: Int,
)

@Serializable data class AssetDetail(
    val asset: AssetSummary,
    val facts: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
    val attributes: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
    @SerialName("current_report") val currentReport: kotlinx.serialization.json.JsonObject? = null,
)

@Serializable data class DictionaryEntry(
    val id: String,
    val category: String? = null,
    val value: String,
    val attributes: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
)

@Serializable data class DictionaryWrite(
    val attributes: Map<String, kotlinx.serialization.json.JsonElement>,
)

@Serializable data class DictionaryCategory(
    val key: String,
    val label: String,
)

@Serializable data class DictionaryField(
    val key: String,
    val label: String,
    val type: String,
    val required: Boolean = false,
    val group: String = "Pozostałe",
    val hint: String? = null,
    val options: List<String> = emptyList(),
    val target: String? = null,
    val format: String? = null,
    val min: Double? = null,
    val max: Double? = null,
)

@Serializable data class DictionarySchema(
    val category: String,
    val label: String,
    val version: Int,
    val fields: List<DictionaryField> = emptyList(),
)

@Serializable data class AssignmentWrite(
    @SerialName("owner_id") val ownerId: String? = null,
    @SerialName("user_id") val userId: String? = null,
    @SerialName("location_id") val locationId: String? = null,
    @SerialName("role_label") val roleLabel: String? = null,
    val place: String? = null,
)

@Serializable data class ChangeEntry(
    val id: String,
    @SerialName("asset_id") val assetId: String,
    val hostname: String,
    @SerialName("occurred_at") val occurredAt: String,
    val category: String,
    val action: String,
    val path: String,
    val label: String,
    @SerialName("old_value") val oldValue: String? = null,
    @SerialName("new_value") val newValue: String? = null,
)

@Serializable data class ReportDefinition(
    val id: String,
    val name: String,
    val type: String,
    val frequency: String,
    val recipients: String,
    val active: Boolean,
    @SerialName("last_status") val lastStatus: String? = null,
    @SerialName("last_sent_at") val lastSentAt: String? = null,
    @SerialName("send_to_vendors") val sendToVendors: Boolean = false,
    val columns: List<String> = emptyList(),
)

@Serializable data class ReportOption(val key: String, val label: String)
@Serializable data class ReportColumn(
    val key: String,
    val label: String,
    val group: String,
    val default: Boolean = false,
)
@Serializable data class ReportCatalog(
    val types: List<ReportOption>,
    val frequencies: List<ReportOption>,
    val columns: List<ReportColumn>,
)
@Serializable data class ReportWrite(
    val name: String,
    val type: String,
    val frequency: String,
    val recipients: String,
    val active: Boolean,
    @SerialName("send_to_vendors") val sendToVendors: Boolean,
    val columns: List<String>,
)

@Serializable data class ApiMessage(val detail: String)

// --- helpdesk ---------------------------------------------------------------
// Zgloszenia chodza po firmach, ktore obsluguje konto, a nie po firmie wybranej
// w aplikacji - dlatego maja wlasny komplet modeli i wlasna nazwe firmy przy
// kazdym zgloszeniu.

@Serializable data class HelpdeskTenant(val id: String, val name: String)

@Serializable data class HelpdeskTechnician(val id: String, val name: String, val email: String)

@Serializable data class HelpdeskCatalog(
    val available: Boolean = false,
    val tenants: List<HelpdeskTenant> = emptyList(),
    val technicians: List<HelpdeskTechnician> = emptyList(),
    val statuses: List<ReportOption> = emptyList(),
    val types: List<ReportOption> = emptyList(),
    val sources: List<ReportOption> = emptyList(),
    val mailbox: Boolean = false,
    @SerialName("closing_email") val closingEmail: Boolean = false,
    @SerialName("attachment_mb") val attachmentMb: Int = 0,
    val waiting: Int = 0,
)

@Serializable data class TicketAsset(
    val id: String,
    val hostname: String,
    val type: String? = null,
    @SerialName("primary_ip") val primaryIp: String? = null,
    @SerialName("tenant_id") val tenantId: String? = null,
)

@Serializable data class Ticket(
    val id: String,
    val number: String,
    val subject: String,
    val status: String,
    @SerialName("status_label") val statusLabel: String,
    val type: String? = null,
    @SerialName("type_label") val typeLabel: String = "",
    @SerialName("tenant_id") val tenantId: String,
    val tenant: String = "",
    @SerialName("requester_email") val requesterEmail: String,
    @SerialName("requester_name") val requesterName: String? = null,
    @SerialName("technician_id") val technicianId: String? = null,
    val technician: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("last_activity") val lastActivity: String? = null,
    @SerialName("closed_at") val closedAt: String? = null,
    val waiting: Boolean = false,
    @SerialName("waiting_since") val waitingSince: String? = null,
    val assets: List<TicketAsset> = emptyList(),
    val minutes: Int = 0,
)

@Serializable data class TicketCounters(
    val open: Int = 0,
    val mine: Int = 0,
    val unassigned: Int = 0,
    val closed: Int = 0,
    val waiting: Int = 0,
)

@Serializable data class TicketPage(
    val items: List<Ticket> = emptyList(),
    val page: Int = 1,
    @SerialName("page_size") val pageSize: Int = 30,
    val total: Int = 0,
    val counters: TicketCounters = TicketCounters(),
)

@Serializable data class TicketAttachment(
    val id: String,
    val name: String,
    val mime: String? = null,
    val size: Long = 0,
    val image: Boolean = false,
)

@Serializable data class TicketEntry(
    val id: String,
    val kind: String,
    val author: String,
    @SerialName("author_email") val authorEmail: String? = null,
    val mine: Boolean = false,
    val content: String = "",
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("sent_at") val sentAt: String? = null,
    val error: String? = null,
    val attachments: List<TicketAttachment> = emptyList(),
)

@Serializable data class TicketField(val label: String, val value: String)

@Serializable data class TicketRequester(
    val email: String,
    val name: String? = null,
    val known: Boolean = false,
    val fields: List<TicketField> = emptyList(),
)

@Serializable data class TicketShare(val technician: String, val minutes: Int, val label: String)

@Serializable data class TicketTime(
    val total: Int = 0,
    @SerialName("total_label") val totalLabel: String = "-",
    val shares: List<TicketShare> = emptyList(),
)

@Serializable data class TicketDetail(
    val ticket: Ticket,
    val entries: List<TicketEntry> = emptyList(),
    val requester: TicketRequester,
    val candidates: List<TicketAsset> = emptyList(),
    val technicians: List<HelpdeskTechnician> = emptyList(),
    val time: TicketTime = TicketTime(),
    @SerialName("closing_email") val closingEmail: Boolean = false,
    val mailbox: Boolean = false,
)

@Serializable data class NewTicket(
    @SerialName("tenant_id") val tenantId: String,
    @SerialName("requester_email") val requesterEmail: String,
    @SerialName("requester_name") val requesterName: String = "",
    val subject: String,
    val content: String,
    val type: String = "",
    val source: String = "telefon",
    @SerialName("asset_id") val assetId: String = "",
    @SerialName("assign_to_me") val assignToMe: Boolean = true,
    @SerialName("notify_customer") val notifyCustomer: Boolean = true,
)

@Serializable data class NewMessage(
    val content: String,
    val kind: String,
    @SerialName("keep_in_progress") val keepInProgress: Boolean = false,
)

@Serializable data class TicketStatusWrite(val status: String, val summary: String = "")
@Serializable data class TicketTechnicianWrite(@SerialName("technician_id") val technicianId: String = "")
@Serializable data class TicketTimeWrite(val minutes: Int, val description: String = "")
@Serializable data class TicketAssetWrite(
    @SerialName("asset_id") val assetId: String,
    val action: String = "attach",
)

/** Odpowiedz na zapis w helpdesku: komunikat dla technika i skutek wysylki. */
@Serializable data class TicketSaved(
    val detail: String = "",
    val sent: Boolean = false,
    val id: String? = null,
    val number: String? = null,
)
