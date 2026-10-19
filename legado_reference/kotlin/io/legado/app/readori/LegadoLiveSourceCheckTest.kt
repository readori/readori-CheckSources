package io.legado.app.readori

import android.app.Application
import android.content.Context
import android.content.res.Resources
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonArray
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.script.ScriptException
import io.legado.app.constant.BookSourceType
import io.legado.app.data.appDb
import io.legado.app.data.entities.Book
import io.legado.app.data.entities.BookSource
import io.legado.app.exception.ContentEmptyException
import io.legado.app.exception.NoStackTraceException
import io.legado.app.exception.TocEmptyException
import io.legado.app.help.DefaultData
import io.legado.app.help.http.okHttpClient
import io.legado.app.help.source.exploreKinds
import io.legado.app.model.CheckSource
import io.legado.app.model.Debug
import io.legado.app.model.webBook.WebBook
import io.legado.app.utils.GSON
import io.legado.app.utils.fromJsonObject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.async
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.mozilla.javascript.WrappedException
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import splitties.init.injectAsAppCtx
import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import kotlin.coroutines.coroutineContext

/**
 * Readori live reference: runs the REAL Legado book-source check over the real
 * network and records a per-source verdict, so Readori's Python validator can
 * be compared against Legado on the very same source package.
 *
 * The flow below is a line-by-line port of
 * `app/src/main/java/io/legado/app/service/CheckSourceService.kt`
 * (checkSource :134, doCheckSource :154, checkBook :201) at the revision that
 * is checked out. The only differences are bookkeeping (stage / count capture)
 * and the executor: Legado's Android service is replaced by plain coroutines.
 * Every engine call (WebBook.*Await, BookSource group helpers, exploreKinds,
 * CheckSource flags, Debug bookkeeping) is the unmodified Legado code.
 *
 * This file is injected into `:app` test sources by `legado-live.init.gradle`;
 * the Legado checkout itself is never modified.
 *
 * System properties (set by the init script from `-PreadoriLive*`):
 *   readori.live.sources         JSON array of Legado book sources (required)
 *   readori.live.output          report JSON path (required)
 *   readori.live.concurrency     parallel sources, default 8
 *   readori.live.timeoutSeconds  per-source timeout; default = CheckSource.timeout (180 s)
 *   readori.live.limit           0 = all
 *   readori.live.offset          skip the first N sources
 *   readori.live.flushEvery      snapshot the report every N finished sources, default 25
 *   readori.live.hardGraceSeconds extra seconds after the timeout before a stuck worker is abandoned, default 60
 *   readori.live.legadoRevision  informational git revision of the checkout
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = Application::class)
class LegadoLiveSourceCheckTest {

    private val outputGson: Gson = GsonBuilder()
        .disableHtmlEscaping()
        .serializeNulls()
        .create()

    @Before
    fun initializeAppContext() {
        primeWindowsRobolectricDefaults()
        val output = requiredProperty("readori.live.output")
        val cacheRoot = File(File(output).absoluteFile.parentFile, "legado-live-context-cache")
        cacheRoot.mkdirs()
        LiveApplication(cacheRoot).apply {
            attachTo(RuntimeEnvironment.getApplication().baseContext)
            injectAsAppCtx()
        }
    }

    /**
     * Legado builds an asset name with File.separator; a Windows-hosted JVM
     * supplies "\" so Room's onOpen callback cannot read the default keyboard
     * assist asset. Local Windows runs only; CI (Linux) is a no-op.
     */
    private fun primeWindowsRobolectricDefaults() {
        if (File.separatorChar != '\\') return
        runCatching {
            val delegateField = DefaultData::class.java
                .getDeclaredField("keyboardAssists\$delegate")
                .apply { isAccessible = true }
            val delegate = delegateField.get(DefaultData)
            val valueField = delegate.javaClass
                .getDeclaredField("_value")
                .apply { isAccessible = true }
            valueField.set(delegate, emptyList<Any>())
        }.onFailure { log("could not prime Windows defaults: $it") }
    }

    @Test
    fun liveSourceCheck() {
        val sourcesFile = File(requiredProperty("readori.live.sources"))
        val outputFile = File(requiredProperty("readori.live.output")).absoluteFile
        val concurrency = intProperty("readori.live.concurrency", 8).coerceAtLeast(1)
        val limit = intProperty("readori.live.limit", 0).coerceAtLeast(0)
        val offset = intProperty("readori.live.offset", 0).coerceAtLeast(0)
        val flushEvery = intProperty("readori.live.flushEvery", 25).coerceAtLeast(1)
        val hardGraceMs = intProperty("readori.live.hardGraceSeconds", 60).coerceAtLeast(0) * 1000L

        // CheckSource.kt:17-22 reads these from CacheManager (appDb); "all check flags ON"
        // is Legado's default full check. Force them so a stale cache row cannot change the run.
        CheckSource.checkSearch = true
        CheckSource.checkDiscovery = true
        CheckSource.checkInfo = true
        CheckSource.checkCategory = true
        CheckSource.checkContent = true
        val timeoutSeconds = intProperty("readori.live.timeoutSeconds", 0)
        if (timeoutSeconds > 0) CheckSource.timeout = timeoutSeconds * 1000L
        val timeoutMs = CheckSource.timeout

        warmUp()

        val loaded = loadSources(sourcesFile)
        val selected = loaded.drop(offset).let { if (limit > 0) it.take(limit) else it }
        log(
            "sources=${loaded.size} selected=${selected.size} offset=$offset limit=$limit " +
                "concurrency=$concurrency timeoutMs=$timeoutMs keyword=${CheckSource.keyword}",
        )
        assertTrue("no sources to check in ${sourcesFile.path}", selected.isNotEmpty())

        val config = JsonObject().apply {
            addProperty("concurrency", concurrency)
            addProperty("timeoutMs", timeoutMs)
            addProperty("hardGraceMs", hardGraceMs)
            addProperty("keyword", CheckSource.keyword)
            addProperty("limit", limit)
            addProperty("offset", offset)
            addProperty("inputCount", loaded.size)
            addProperty("selectedCount", selected.size)
        }
        val reporter = Reporter(
            outputFile = outputFile,
            total = selected.size,
            flushEvery = flushEvery,
            config = config,
            legadoRevision = System.getProperty("readori.live.legadoRevision").orEmpty(),
            gson = outputGson,
        )
        reporter.flushSnapshot(partial = true)

        val workPool = Executors.newCachedThreadPool { runnable ->
            // Rhino-heavy rules recurse deeply; 8 MiB matches the main-thread default.
            Thread(null, runnable, "readori-live-check", 8L * 1024 * 1024).apply { isDaemon = true }
        }
        val workDispatcher = workPool.asCoroutineDispatcher()
        try {
            runBlocking {
                val semaphore = Semaphore(concurrency)
                val jobs = selected.mapIndexed { index, entry ->
                    launch(Dispatchers.Default) {
                        semaphore.withPermit {
                            val json = checkOne(index, entry, timeoutMs, hardGraceMs, workDispatcher)
                            reporter.record(index, json)
                        }
                    }
                }
                jobs.joinAll()
            }
        } finally {
            reporter.flushSnapshot(partial = false)
            workPool.shutdownNow()
        }
        log("done: ${reporter.summaryLine()} output=${outputFile.path}")
    }

    /** Touch the lazily-built singletons on the test thread so concurrent workers never race their init. */
    private fun warmUp() {
        runCatching { GSON }.onFailure { log("warm-up GSON: $it") }
        runCatching { appDb.cacheDao }.onFailure { log("warm-up appDb: $it") }
        runCatching { okHttpClient }.onFailure { log("warm-up okHttpClient: $it") }
    }

    // ----------------------------------------------------------------- loading

    /** One entry per element of the input array: a parsed source, or an import error. */
    private class Loaded(val index: Int, val source: BookSource?, val importError: String?, val raw: JsonObject?)

    /**
     * Legado imports with its own `GSON` (ImportBookSourceViewModel.importSource: `GSON.fromJsonArray<BookSource>`).
     * Elements are parsed one by one with that same GSON (so every Legado type adapter and field default applies)
     * so that a single malformed source is reported instead of rejecting the whole package.
     */
    private fun loadSources(file: File): List<Loaded> {
        val text = String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8)
            .removePrefix("﻿")
            .trim()
        val root = JsonParser.parseString(text)
        val array: JsonArray = when {
            root.isJsonArray -> root.asJsonArray
            root.isJsonObject -> JsonArray().also { it.add(root) }
            else -> error("sources file must hold a JSON array of book sources")
        }
        val out = ArrayList<Loaded>(array.size())
        array.forEachIndexed { index, element ->
            val raw = element.takeIf { it.isJsonObject }?.asJsonObject
            if (raw == null) {
                out += Loaded(index, null, "not a JSON object", null)
                return@forEachIndexed
            }
            val parsed = GSON.fromJsonObject<BookSource>(element.toString())
            val source = parsed.getOrNull()
            when {
                source == null ->
                    out += Loaded(index, null, "import: ${parsed.exceptionOrNull()?.message.orEmpty()}", raw)
                // ImportBookSourceViewModel.kt: `source.bookSourceUrl.isEmpty()` -> "不是书源"
                source.bookSourceUrl.isEmpty() -> out += Loaded(index, null, "import: not a book source (empty bookSourceUrl)", raw)
                else -> out += Loaded(index, source, null, raw)
            }
        }
        return out
    }

    // ------------------------------------------------------------ one source

    private suspend fun checkOne(
        index: Int,
        entry: Loaded,
        timeoutMs: Long,
        hardGraceMs: Long,
        workDispatcher: kotlinx.coroutines.CoroutineDispatcher,
    ): JsonObject {
        val source = entry.source
        val rec = Rec(
            index = entry.index,
            url = source?.bookSourceUrl ?: entry.raw?.stringOrEmpty("bookSourceUrl").orEmpty(),
            name = source?.bookSourceName ?: entry.raw?.stringOrEmpty("bookSourceName").orEmpty(),
        )
        val started = System.nanoTime()
        if (source == null) {
            rec.importError = entry.importError
            rec.stage = "import"
            rec.error = entry.importError
            rec.elapsedMs = 0
            return finalizeRec(rec, null)
        }
        rec.webViewDeclared = declaresWebView(source)
        val work = CoroutineScope(workDispatcher).async { checkSource(source, rec, timeoutMs) }
        val finished = try {
            withTimeoutOrNull(timeoutMs + hardGraceMs) {
                work.await()
                true
            } ?: false
        } catch (t: Throwable) {
            // checkSource only rethrows cancellation; anything else is a harness bug worth seeing.
            if (rec.failure == null) rec.failure = t
            true
        }
        if (!finished) {
            work.cancel()
            rec.hardTimeout = true
            if (rec.failure == null) {
                rec.failure = IllegalStateException(
                    "hardTimeout: worker did not stop ${(timeoutMs + hardGraceMs) / 1000}s after start",
                )
            }
            if (rec.failureGroup == null) rec.failureGroup = "校验超时"
        }
        rec.elapsedMs = (System.nanoTime() - started) / 1_000_000
        return finalizeRec(rec, source)
    }

    // ------------------------------------------ port of CheckSourceService.kt

    /** CheckSourceService.kt:134-152 (checkSource). */
    private suspend fun checkSource(source: BookSource, rec: Rec, timeoutMs: Long) {
        kotlin.runCatching {
            withTimeout(timeoutMs) {
                doCheckSource(source, rec)
            }
        }.onSuccess {
            synchronized(Debug) { Debug.updateFinalMessage(source.bookSourceUrl, "校验成功") }
        }.onFailure {
            coroutineContext.ensureActive()
            rec.failure = it
            // a failure that was only collected as groups (rec.finalGroupThrown) must not mark the last stage failed
            if (!rec.finalGroupThrown) markStageFailed(rec)
            when (it) {
                is TimeoutCancellationException -> {
                    source.addGroup("校验超时")
                    rec.failureGroup = "校验超时"
                }

                is ScriptException, is WrappedException -> {
                    source.addGroup("js失效")
                    rec.failureGroup = "js失效"
                }

                !is NoStackTraceException -> {
                    source.addGroup("网站失效")
                    rec.failureGroup = "网站失效"
                }
            }
            source.addErrorComment(it)
            synchronized(Debug) {
                Debug.updateFinalMessage(source.bookSourceUrl, "校验失败:${it.localizedMessage}")
            }
        }
        rec.lastDebug = synchronized(Debug) { Debug.debugMessageMap[source.bookSourceUrl] }
    }

    /** CheckSourceService.kt:154-196 (doCheckSource). */
    private suspend fun doCheckSource(source: BookSource, rec: Rec) {
        synchronized(Debug) { Debug.startChecking(source) }
        source.removeInvalidGroups()
        source.removeErrorComment()
        //校验搜索书籍 (:158-173)
        if (CheckSource.checkSearch) {
            val searchWord = source.getCheckKeyword(CheckSource.keyword)
            rec.keyword = searchWord
            if (!source.searchUrl.isNullOrBlank()) {
                source.removeGroup("搜索链接规则为空")
                rec.stage = "search"
                val searchBooks = WebBook.searchBookAwait(source, searchWord)
                rec.searchCount = searchBooks.size
                if (searchBooks.isEmpty()) {
                    source.addGroup("搜索失效")
                    rec.search = false
                    rec.groupFailures += "search"
                } else {
                    source.removeGroup("搜索失效")
                    rec.search = true
                    checkBook(searchBooks.first().toBook(), source, rec, true)
                }
            } else {
                source.addGroup("搜索链接规则为空")
                rec.searchSkipped = true
            }
        }
        //校验发现书籍 (:174-191)
        if (CheckSource.checkDiscovery && !source.exploreUrl.isNullOrBlank()) {
            rec.stage = "explore.kinds"
            val url = source.exploreKinds().firstOrNull {
                !it.url.isNullOrBlank()
            }?.url
            if (url.isNullOrBlank()) {
                source.addGroup("发现规则为空")
                rec.exploreSkipped = true
            } else {
                source.removeGroup("发现规则为空")
                rec.stage = "explore"
                val exploreBooks = WebBook.exploreBookAwait(source, url)
                rec.exploreCount = exploreBooks.size
                if (exploreBooks.isEmpty()) {
                    source.addGroup("发现失效")
                    rec.explore = false
                    rec.groupFailures += "explore"
                } else {
                    source.removeGroup("发现失效")
                    rec.explore = true
                    checkBook(exploreBooks.first().toBook(), source, rec, false)
                }
            }
        }
        // (:192-195) a failure that was only recorded as a group still fails the source
        val finalCheckMessage = source.getInvalidGroupNames()
        rec.invalidGroups = finalCheckMessage
        if (finalCheckMessage.isNotBlank()) {
            rec.finalGroupThrown = true
            throw NoStackTraceException(finalCheckMessage)
        }
    }

    /** CheckSourceService.kt:198-242 (checkBook): detail, TOC and content of the first book. */
    private suspend fun checkBook(book: Book, source: BookSource, rec: Rec, isSearchBook: Boolean) {
        val prefix = if (isSearchBook) "search" else "explore"
        val chain = if (isSearchBook) rec.searchChain else rec.exploreChain
        kotlin.runCatching {
            if (!CheckSource.checkInfo) {
                return
            }
            //校验详情 (:206-209)
            if (book.tocUrl.isBlank()) {
                rec.stage = "$prefix.info"
                WebBook.getBookInfoAwait(source, book)
            }
            chain.info = true
            if (!CheckSource.checkCategory || source.bookSourceType == BookSourceType.file) {
                return
            }
            //校验目录 (:213-218) - the full list is only kept to record its size
            rec.stage = "$prefix.toc"
            val fullToc = WebBook.getChapterListAwait(source, book).getOrThrow()
            chain.tocCount = fullToc.size
            val toc = fullToc.asSequence()
                .filter { !(it.isVolume && it.url.startsWith(it.title)) }
                .take(2)
                .toList()
            val nextChapterUrl = toc.getOrNull(1)?.url ?: toc.first().url
            chain.toc = true
            if (!CheckSource.checkContent) {
                return
            }
            //校验正文 (:222-229)
            rec.stage = "$prefix.content"
            val content = WebBook.getContentAwait(
                bookSource = source,
                book = book,
                bookChapter = toc.first(),
                nextChapterUrl = nextChapterUrl,
                needSave = false
            )
            chain.contentChars = content.length
            chain.content = true
        }.onFailure {
            markStageFailed(rec)
            val bookType = if (isSearchBook) "搜索" else "发现"
            // TocEmptyException builds its message from R.string.chapter_list_empty. If the Robolectric run has no
            // merged resources (`-PreadoriLiveIncludeResources=false`) that lookup throws NotFoundException instead;
            // it can only come from the TOC-empty branch, so treat it as that exception.
            val tocEmptyWithoutResources =
                it is Resources.NotFoundException && rec.stage == "$prefix.toc"
            when {
                it is ContentEmptyException -> {
                    source.addGroup("${bookType}正文失效")
                    rec.groupFailures += "$prefix.content"
                }

                it is TocEmptyException || tocEmptyWithoutResources -> {
                    source.addGroup("${bookType}目录失效")
                    rec.groupFailures += "$prefix.toc"
                }

                else -> throw it
            }
        }.onSuccess {
            val bookType = if (isSearchBook) "搜索" else "发现"
            source.removeGroup("${bookType}目录失效")
            source.removeGroup("${bookType}正文失效")
        }
    }

    // ----------------------------------------------------------- bookkeeping

    private class Chain {
        var info: Boolean? = null
        var toc: Boolean? = null
        var content: Boolean? = null
        var tocCount: Int = 0
        var contentChars: Int = 0
    }

    private class Rec(val index: Int, val url: String, val name: String) {
        @Volatile var stage: String = ""
        var search: Boolean? = null
        var explore: Boolean? = null
        val searchChain = Chain()
        val exploreChain = Chain()
        var searchCount = 0
        var exploreCount = 0
        var searchSkipped = false
        var exploreSkipped = false
        var keyword = ""
        var failure: Throwable? = null
        var failureGroup: String? = null
        var invalidGroups = ""
        var finalGroupThrown = false
        val groupFailures = ArrayList<String>()
        var hardTimeout = false
        var importError: String? = null
        var error: String? = null
        var webViewDeclared = false
        var lastDebug: String? = null
        var elapsedMs = 0L
    }

    private fun markStageFailed(rec: Rec) {
        when (rec.stage) {
            "search" -> rec.search = false
            "explore" -> rec.explore = false
            "search.info" -> rec.searchChain.info = false
            "search.toc" -> rec.searchChain.toc = false
            "search.content" -> rec.searchChain.content = false
            "explore.info" -> rec.exploreChain.info = false
            "explore.toc" -> rec.exploreChain.toc = false
            "explore.content" -> rec.exploreChain.content = false
        }
    }

    private fun declaresWebView(source: BookSource): Boolean {
        val candidates = listOf(source.searchUrl, source.exploreUrl, source.ruleContent?.webJs)
        return candidates.any { !it.isNullOrBlank() && (it.contains("webView") || it.contains("webJs")) } ||
            !source.ruleContent?.webJs.isNullOrBlank()
    }

    /** Failure causes that point at the JVM / Robolectric harness rather than at the source. */
    private fun harnessSuspect(failure: Throwable?): String? {
        var current = failure
        var depth = 0
        while (current != null && depth++ < 8) {
            val className = current.javaClass.name
            val message = current.message.orEmpty()
            when {
                current is NoClassDefFoundError || current is ExceptionInInitializerError ||
                    current is UnsatisfiedLinkError -> return "jvm-linkage:$className"

                current is Resources.NotFoundException -> return "android-resource"
                className.contains("WebView", ignoreCase = true) ||
                    message.contains("WebView", ignoreCase = true) -> return "webview"

                message.contains("Looper", ignoreCase = true) ||
                    message.contains("Robolectric", ignoreCase = true) -> return "robolectric"
            }
            current = current.cause
        }
        return null
    }

    private fun finalizeRec(rec: Rec, source: BookSource?): JsonObject {
        val failure = rec.failure
        val ok = failure == null && rec.importError == null
        val failureStage: String? = when {
            ok -> null
            rec.importError != null -> "import"
            failure is NoStackTraceException && rec.finalGroupThrown && rec.groupFailures.isNotEmpty() ->
                rec.groupFailures.joinToString("+")

            else -> rec.stage.ifEmpty { "unknown" }
        }
        val errorText = rec.error ?: failure?.let {
            "${it.javaClass.name}: ${it.message.orEmpty()}".take(500)
        }
        val primary = if (rec.searchChain.info != null) rec.searchChain else rec.exploreChain
        val vacuous = ok && rec.search == null && rec.explore == null
        return JsonObject().apply {
            addProperty("index", rec.index)
            addProperty("bookSourceUrl", rec.url)
            addProperty("bookSourceName", rec.name)
            addProperty("ok", ok)
            add("stages", JsonObject().apply {
                addNullableBoolean("search", rec.search)
                addNullableBoolean("explore", rec.explore)
                addNullableBoolean("info", primary.info)
                addNullableBoolean("toc", primary.toc)
                addNullableBoolean("content", primary.content)
            })
            addProperty("searchCount", rec.searchCount)
            addProperty("exploreCount", rec.exploreCount)
            addProperty("tocCount", primary.tocCount)
            addProperty("contentChars", primary.contentChars)
            add("searchChain", chainJson(rec.searchChain))
            add("exploreChain", chainJson(rec.exploreChain))
            addProperty("failureStage", failureStage)
            addProperty("failureGroup", rec.failureGroup)
            addProperty("invalidGroups", rec.invalidGroups)
            addProperty("error", errorText)
            addProperty("elapsedMs", rec.elapsedMs)
            addProperty("keyword", rec.keyword)
            addProperty("vacuous", vacuous)
            addProperty("searchSkipped", rec.searchSkipped)
            addProperty("exploreSkipped", rec.exploreSkipped)
            addProperty("hardTimeout", rec.hardTimeout)
            addProperty("webViewDeclared", rec.webViewDeclared)
            addProperty("harnessSuspect", if (ok) null else harnessSuspect(failure))
            addProperty("lastDebug", rec.lastDebug)
            addProperty("finalGroup", source?.bookSourceGroup)
        }
    }

    private fun chainJson(chain: Chain): JsonObject = JsonObject().apply {
        addNullableBoolean("info", chain.info)
        addNullableBoolean("toc", chain.toc)
        addNullableBoolean("content", chain.content)
        addProperty("tocCount", chain.tocCount)
        addProperty("contentChars", chain.contentChars)
    }

    private fun JsonObject.addNullableBoolean(name: String, value: Boolean?) {
        if (value == null) add(name, JsonNull.INSTANCE) else addProperty(name, value)
    }

    private fun JsonObject.stringOrEmpty(name: String): String =
        get(name)?.takeIf { it.isJsonPrimitive }?.asString.orEmpty()

    // -------------------------------------------------------------- reporting

    /** Collects finished results and rewrites the report file every [flushEvery] sources (atomic rename). */
    private class Reporter(
        private val outputFile: File,
        total: Int,
        private val flushEvery: Int,
        private val config: JsonObject,
        private val legadoRevision: String,
        private val gson: Gson,
    ) {
        private val results = arrayOfNulls<JsonObject>(total)
        private val finished = AtomicInteger(0)
        private val startedAt: Instant = Instant.now()
        private val startNanos = System.nanoTime()

        @Synchronized
        fun record(index: Int, json: JsonObject) {
            results[index] = json
            val done = finished.incrementAndGet()
            val ok = json.get("ok")?.asBoolean == true
            val stage = json.get("failureStage")?.takeIf { !it.isJsonNull }?.asString
            val error = json.get("error")?.takeIf { !it.isJsonNull }?.asString.orEmpty().take(160)
            println(
                "[LEGADO-LIVE] $done/${results.size} ok=$ok " +
                    "${json.get("elapsedMs")?.asLong ?: 0}ms " +
                    "${json.get("bookSourceName")?.asString.orEmpty()} " +
                    "(${json.get("bookSourceUrl")?.asString.orEmpty()})" +
                    (if (ok) "" else " stage=$stage error=$error"),
            )
            if (done % flushEvery == 0) flushSnapshot(partial = true)
        }

        @Synchronized
        fun flushSnapshot(partial: Boolean) {
            val resultArray = JsonArray()
            var ok = 0
            var failed = 0
            var timeouts = 0
            var js = 0
            var site = 0
            var harness = 0
            var vacuous = 0
            val byStage = JsonObject()
            for (entry in results) {
                entry ?: continue
                resultArray.add(entry)
                if (entry.get("ok")?.asBoolean == true) {
                    ok++
                    if (entry.get("vacuous")?.asBoolean == true) vacuous++
                } else {
                    failed++
                    when (entry.get("failureGroup")?.takeIf { !it.isJsonNull }?.asString) {
                        "校验超时" -> timeouts++
                        "js失效" -> js++
                        "网站失效" -> site++
                    }
                    if (entry.get("harnessSuspect")?.takeIf { !it.isJsonNull } != null) harness++
                    val stage = entry.get("failureStage")?.takeIf { !it.isJsonNull }?.asString ?: "unknown"
                    byStage.addProperty(stage, (byStage.get(stage)?.asInt ?: 0) + 1)
                }
            }
            val summary = JsonObject().apply {
                addProperty("total", results.size)
                addProperty("completed", resultArray.size())
                addProperty("passed", ok)
                addProperty("failed", failed)
                addProperty("vacuousPassed", vacuous)
                addProperty("timeouts", timeouts)
                addProperty("jsFailures", js)
                addProperty("siteFailures", site)
                addProperty("harnessSuspects", harness)
                add("failuresByStage", byStage)
            }
            val document = JsonObject().apply {
                addProperty("schemaVersion", 1)
                addProperty("engine", "legado-live")
                addProperty("flow", "CheckSourceService.checkSource (search, discovery, info, toc, content)")
                addProperty("legadoRevision", legadoRevision)
                addProperty("partial", partial)
                addProperty("startedAt", startedAt.toString())
                addProperty("generatedAt", Instant.now().toString())
                addProperty("elapsedMs", (System.nanoTime() - startNanos) / 1_000_000)
                add("config", config)
                add("summary", summary)
                add("results", resultArray)
            }
            writeAtomically(gson.toJson(document) + "\n")
        }

        @Synchronized
        fun summaryLine(): String {
            val done = results.count { it != null }
            val passed = results.count { it?.get("ok")?.asBoolean == true }
            return "completed=$done/${results.size} passed=$passed failed=${done - passed}"
        }

        private fun writeAtomically(text: String) {
            outputFile.parentFile?.mkdirs()
            val temp = File(outputFile.parentFile, outputFile.name + ".tmp")
            temp.writeText(text, Charsets.UTF_8)
            try {
                Files.move(
                    temp.toPath(),
                    outputFile.toPath(),
                    StandardCopyOption.REPLACE_EXISTING,
                    StandardCopyOption.ATOMIC_MOVE,
                )
            } catch (_: Exception) {
                Files.move(temp.toPath(), outputFile.toPath(), StandardCopyOption.REPLACE_EXISTING)
            }
        }
    }

    // ---------------------------------------------------------------- helpers

    private fun log(message: String) {
        println("[LEGADO-LIVE] $message")
    }

    private fun requiredProperty(name: String): String =
        System.getProperty(name)?.takeIf { it.isNotBlank() }
            ?: error("Missing JVM system property: $name")

    private fun intProperty(name: String, fallback: Int): Int =
        System.getProperty(name)?.trim()?.toIntOrNull() ?: fallback

    /** Same shape as the differential harness: Robolectric's app, but with a writable cache root. */
    private class LiveApplication(private val cacheRoot: File) : Application() {
        fun attachTo(context: Context) {
            attachBaseContext(context)
        }

        override fun getCacheDir(): File = cacheRoot

        override fun getFilesDir(): File = cacheRoot

        override fun getExternalCacheDir(): File = cacheRoot
    }
}
