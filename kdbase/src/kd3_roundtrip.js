// JScript ES3 для cscript. Аргументы: команда, копия базы, результат, затем аргументы команды.
// selftest: команда, результат, строка. Соединения с 1С в этой команде нет.
// Результат: UTF-8 без BOM, ключ<TAB>значение<LF>; в значениях экранируются \\, \t, \r, \n.
// Эталон: reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:9,195,709-795.
// Загрузка: reference/kd3-cfg/DataProcessors/ЗагрузкаМодуляМенеджера/Ext/ManagerModule.bsl:2,61-153.

var rows = [];
var resultPath = "";
var shell = new ActiveXObject("WScript.Shell");
var environment = shell.Environment("PROCESS");
var user = environment("KD3_USER");
var password = environment("KD3_PASSWORD");
// База БСП для base-export может иметь другого пользователя: KD3_FROM_USER / KD3_FROM_PASSWORD.
var fromUser = environment("KD3_FROM_USER");
var fromPassword = environment("KD3_FROM_PASSWORD");

function redact(text) {
    var values = [user, password];
    for (var i = 0; i < values.length; i++) {
        if (values[i] !== "") {
            text = text.split(values[i].replace(/"/g, '""')).join("<скрыто>");
            text = text.split(values[i]).join("<скрыто>");
        }
    }
    return text;
}

function record(key, value) {
    var text = redact(String(value));
    text = text.replace(/\\/g, "\\\\").replace(/\t/g, "\\t");
    text = text.replace(/\r/g, "\\r").replace(/\n/g, "\\n");
    rows.push(key + "\t" + text);
}

function writeUtf8(path, text) {
    var stream = new ActiveXObject("ADODB.Stream");
    stream.Type = 2;
    stream.Charset = "utf-8";
    stream.Open();
    stream.WriteText(text);
    stream.Position = 0;
    stream.Type = 1;
    stream.Position = 3; // ADODB добавляет BOM: в результирующий файл он не переносится.
    var binary = new ActiveXObject("ADODB.Stream");
    binary.Type = 1;
    binary.Open();
    stream.CopyTo(binary);
    binary.SaveToFile(path, 2);
    binary.Close();
    stream.Close();
}

function quoteConnection(value) {
    return '"' + value.replace(/"/g, '""') + '"';
}

function connect(path) {
    var connection = "File=" + quoteConnection(path) + ";";
    if (user !== "") connection += "Usr=" + quoteConnection(user) + ";";
    if (password !== "") connection += "Pwd=" + quoteConnection(password) + ";";
    var connector = new ActiveXObject("V83.COMConnector");
    return connector.Connect(connection);
}

function conversions(base) {
    var query = base.NewObject("Запрос");
    query.Текст = "ВЫБРАТЬ К.Ссылка КАК Ссылка, К.Наименование КАК Наименование " +
        "ИЗ Справочник.Конвертации КАК К ГДЕ НЕ К.ЭтоГруппа И НЕ К.ПометкаУдаления " +
        "УПОРЯДОЧИТЬ ПО К.Наименование";
    var selection = query.Выполнить().Выбрать();
    var found = [];
    while (selection.Следующий()) {
        found.push({ ref: selection.Ссылка, name: String(selection.Наименование) });
    }
    return found;
}

function findConversion(base, all, name) {
    var found = null;
    for (var i = 0; i < all.length; i++) {
        if (all[i].name === name) {
            if (found !== null) throw new Error("Неоднозначное имя конвертации: " + name);
            found = all[i].ref;
        }
    }
    if (found === null) throw new Error("Не найдена конвертация: " + name);
    return found;
}

function scalar(base, text, ref) {
    var query = base.NewObject("Запрос");
    query.Текст = text;
    query.УстановитьПараметр("К", ref);
    var selection = query.Выполнить().Выбрать();
    if (!selection.Следующий()) throw new Error("Запрос состава не вернул строку");
    return selection.Получить(0);
}

function describe(base, ref, index) {
    var prefix = "conversion." + index + ".";
    var object = ref.ПолучитьОбъект();
    record(prefix + "name", object.Наименование);
    record(prefix + "manager_version", object.ВерсияФорматаМенеджера);
    var versions = [];
    var enumerator = new Enumerator(object.ВерсииФормата);
    for (; !enumerator.atEnd(); enumerator.moveNext()) {
        versions.push(String(base.String(enumerator.item().ВерсияФормата)));
    }
    record(prefix + "format_version", versions.join(","));
    var catalogs = ["ПравилаКонвертацииОбъектов", "ПравилаОбработкиДанных",
        "ПравилаКонвертацииПредопределенныхДанных", "Алгоритмы"];
    var kinds = ["pko", "pod", "pkpd", "algorithms"];
    for (var k = 0; k < catalogs.length; k++) {
        var count = scalar(base,
            "ВЫБРАТЬ КОЛИЧЕСТВО(РАЗЛИЧНЫЕ П.Ссылка) ИЗ Справочник." + catalogs[k] + " КАК П " +
            "ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.СоставыКонвертаций КАК С " +
            "ПО С.ЭлементКонвертации = П.Ссылка И С.Владелец = &К И С.Отключить = ЛОЖЬ " +
            "ГДЕ П.ПометкаУдаления = ЛОЖЬ И П.ВременнаяКопия = ЛОЖЬ", ref);
        record(prefix + "count." + kinds[k], count);
    }
    var propertyQuery = "ВЫБРАТЬ КОЛИЧЕСТВО(РАЗЛИЧНЫЕ ПКС.Ссылка) " +
        "ИЗ Справочник.ПравилаКонвертацииСвойств КАК ПКС " +
        "ГДЕ ПКС.ПометкаУдаления = ЛОЖЬ И ";
    var owners = "ПКС.Владелец В (ВЫБРАТЬ С.ЭлементКонвертации " +
        "ИЗ Справочник.СоставыКонвертаций КАК С ГДЕ С.Владелец = &К И С.Отключить = ЛОЖЬ)";
    record(prefix + "count.pks", scalar(base, propertyQuery + "НЕ ПКС.ЭтоГруппа И " + owners, ref));
    record(prefix + "count.pktch", scalar(base, propertyQuery + "ПКС.ЭтоГруппа И " + owners, ref));
}

function exportModule(base, ref, path) {
    if (ref.ПолучитьОбъект().ИспользоватьРазделениеМодуля) {
        throw new Error("Разделённые модули не поддерживаются этим адаптером");
    }
    var processor = base.Обработки.ВыгрузкаМодуля.Создать();
    processor.Конвертация = ref;
    var result = processor.ВыполнитьВыгрузкуМодулей();
    if (result.Количество() !== 1) throw new Error("Ожидался один модуль менеджера");
    var text = result.МенеджерОбменаЧерезУниверсальныйФормат.ПолучитьТекст();
    writeUtf8(path, String(text));
}

function roundtrip(base, all, input, name, sample, output) {
    for (var i = 0; i < all.length; i++) {
        if (all[i].name === name) throw new Error("Имя новой конвертации уже занято");
    }
    var sampleRef = findConversion(base, all, sample);
    var parameters = base.NewObject("Структура");
    parameters.Вставить("ИсточникЗагрузки", 1);
    var files = base.NewObject("Массив");
    var file = base.NewObject("Структура");
    // input уже скопирован Python: загрузчик удаляет файл Хранение (эталон: :277).
    file.Вставить("ПолноеИмя", input);
    file.Вставить("Хранение", input);
    file.Вставить("ЭтоРасширение", false);
    files.Добавить(file);
    parameters.Вставить("ПомещенныеФайлы", files);
    parameters.Вставить("Конвертация", base.Справочники.Конвертации.ПустаяСсылка());
    parameters.Вставить("СоздатьНовуюКонвертацию", true);
    parameters.Вставить("ИмяНовойКонвертации", name);
    parameters.Вставить("Конфигурация", sampleRef.Конфигурация);
    parameters.Вставить("ИмяМенеджераКонвертации", "МенеджерОбменаЧерезУниверсальныйФормат");
    parameters.Вставить("ТолькоОбработчики", false);
    parameters.Вставить("ИтерацияЗагрузки", 1);
    var address = base.ПоместитьВоВременноеХранилище(undefined, base.NewObject("УникальныйИдентификатор"));
    base.Обработки.ЗагрузкаМодуляМенеджера.ВыполнитьЗагрузкуМодуляМенеджера(parameters, address);
    var result = base.ПолучитьИзВременногоХранилища(address);
    if (!result.Успешно) throw new Error("Загрузка КД 3: " + String(result.Ошибки));
    if (String(result.Ошибки) !== "") throw new Error("Загрузка КД 3: " + String(result.Ошибки));
    // Сообщение необязательно (эталон: :284-288); числа из него не используются.
    if (result.Свойство("СообщениеОРезультате")) {
        record("loader_message", result.СообщениеОРезультате);
    }
    describe(base, result.Конвертация, 0);
    exportModule(base, result.Конвертация, output);
    record("conversion_count", 1);
}

// XDTOFactory/ctors/ctor118.html, methods/ExportXMLSchema3309.html в shcntx_ru.hbk:
// модель {http://v8.1c.ru/8.1/xdto}Model и коллекция импортируемых пакетов.
// Схемы пишет платформа: XMLSchema.UpdateDOMElement, DOMWriter.Write (там же).
function packageUris(base, factory, requested) {
    var uris = base.NewObject("Массив");
    var seen = {};
    function visit(uri) {
        if (uri === "http://www.w3.org/2001/XMLSchema" || seen["uri:" + uri]) return;
        seen["uri:" + uri] = true;
        var packet = factory.Пакеты.Получить(uri);
        if (packet === undefined || packet === null) throw new Error("Нет пакета XDTO: " + uri);
        uris.Добавить(uri);
        var deps = new Enumerator(packet.Зависимости);
        for (; !deps.atEnd(); deps.moveNext()) visit(String(deps.item().URIПространстваИмен));
    }
    for (var i = 0; i < requested.length; i++) visit(requested[i]);
    return uris;
}

function exportSchemas(base, factory, uris, folder, extensionUri) {
    var schemas = factory.ЭкспортСхемыXML(uris);
    if (schemas.Количество() === 0) throw new Error("Платформа не вернула XSD");
    var extensionFound = false;
    for (var i = 0; i < schemas.Количество(); i++) {
        var schema = schemas.Получить(i);
        var uri = String(schema.ПространствоИмен);
        var path = folder + "\\schema-" + i + ".xsd";
        if (uri === extensionUri) {
            path = folder + "\\extension.xsd";
            extensionFound = true;
        }
        schema.ОбновитьЭлементDOM();
        var writer = base.NewObject("ЗаписьXML");
        writer.ОткрытьФайл(path);
        base.NewObject("ЗаписьDOM").Записать(schema.ЭлементDOM, writer);
        writer.Закрыть();
        record("schema." + i + ".path", path);
        record("schema." + i + ".namespace", uri);
    }
    if (extensionUri !== "" && !extensionFound) throw new Error("Нет XSD собственного URI");
    record("schema_count", schemas.Количество());
}

// reference/kd3-cfg/DataProcessors/ЗагрузкаСтруктурыФормата/Forms/Форма/Ext/Form/Module.bsl:
// 298-310 — номер и родитель из URI; 322-350 — параметры серверного загрузчика.
// .../ЗагрузкаСтруктурыФормата/Ext/ManagerModule.bsl:8-12,68-86,311 — вызов и результат.
function loadFormat(base, files, uri, version, extensionPath) {
    var processor = base.Обработки.ЗагрузкаСтруктурыФормата.Создать();
    processor.ИмяОсновногоПакетаXDTO = uri;
    processor.ИмяФайлаРасширенияФормата = extensionPath;
    processor.ДобавлятьТолькоНовыеОбъектыСвойстваЗначения = false;
    var ref = base.Справочники.ВерсииФормата.НайтиПоРеквизиту("ПространствоИмен", uri);
    processor.ВерсияФормата = ref;
    var slash = uri.lastIndexOf("/");
    // У произвольного URI нет номера после '/': группа и номер задаются явно,
    // чтобы группа не получила то же ПространствоИмен, что и элемент версии.
    var template = slash >= 0 ? uri.substring(0, slash) : uri + "/versions";
    var number = slash >= 0 ? uri.substring(slash + 1) : version;
    var parent = base.Справочники.ВерсииФормата.НайтиПоРеквизиту(
        "ПространствоИмен", template, base.Справочники.ВерсииФормата.ПустаяСсылка());
    var parameters = base.NewObject("Структура");
    parameters.Вставить("СпособЗагрузки", ref.Пустая() ? 0 : 1);
    parameters.Вставить("ВерсияФормата", processor.ВерсияФормата);
    parameters.Вставить("ДобавлятьТолькоНовые", false);
    parameters.Вставить("ЭтоРасширение", extensionPath !== "");
    parameters.Вставить("ИмяОсновногоПакетаXDTO", processor.ИмяОсновногоПакетаXDTO);
    parameters.Вставить("РодительВерсии", parent);
    parameters.Вставить("ШаблонПространстваИмен", template);
    parameters.Вставить("НомерВерсииФормата", number);
    parameters.Вставить("ДанныеДляЗагрузки", files);
    var address = base.ПоместитьВоВременноеХранилище(undefined, base.NewObject("УникальныйИдентификатор"));
    base.Обработки.ЗагрузкаСтруктурыФормата.ВыполнитьЗагрузкуФормата(parameters, address);
    var result = base.ПолучитьИзВременногоХранилища(address);
    if (result === undefined || result === null || !result.Успех) {
        throw new Error("Загрузка структуры формата КД 3 не выполнена: " + uri);
    }
    return result.ВерсияФормата;
}

function formatLoad(base, input, uri, baseUri, version, folder, filenames) {
    var files = base.NewObject("Массив");
    for (var i = 0; i < filenames.length; i++) files.Добавить(filenames[i]);
    var imported = base.СоздатьФабрикуXDTO(files);
    // XML Package.bin — объект Package модели типов, а не сама XSD.
    var reader = base.NewObject("ЧтениеXML");
    reader.ОткрытьФайл(input);
    var packet = base.ФабрикаXDTO.ПрочитатьXML(reader,
        base.ФабрикаXDTO.Тип("http://v8.1c.ru/8.1/xdto", "Package"));
    reader.Закрыть();
    var model = base.ФабрикаXDTO.Создать(base.ФабрикаXDTO.Тип("http://v8.1c.ru/8.1/xdto", "Model"));
    model.package.Добавить(packet);
    var factory = base.NewObject("ФабрикаXDTO", model, imported.Пакеты);
    exportSchemas(base, factory, uri, folder, uri);
    // Загрузчик КД 3 удаляет переданные файлы схем после успешной загрузки
    // (ЗагрузкаСтруктурыФормата/Ext/ManagerModule.bsl:303-309): каждой загрузке — свои копии,
    // имена файлов сохраняются (schemaLocation может ссылаться на соседа).
    var fso = new ActiveXObject("Scripting.FileSystemObject");
    function stageFiles(name, extra) {
        var stage = folder + "\\" + name;
        if (!fso.FolderExists(stage)) fso.CreateFolder(stage);
        var staged = base.NewObject("Массив");
        var all = filenames.slice(0);
        if (extra !== "") all.push(extra);
        for (var k = 0; k < all.length; k++) {
            var target = stage + "\\" + fso.GetFileName(all[k]);
            fso.CopyFile(all[k], target, true);
            staged.Добавить(target);
        }
        return staged;
    }
    var baseRef = base.Справочники.ВерсииФормата.НайтиПоРеквизиту("ПространствоИмен", baseUri);
    if (baseRef.Пустая()) {
        loadFormat(base, stageFiles("load-base", ""), baseUri, version, "");
        record("base_loaded", true);
    } else {
        record("base_loaded", false);
    }
    var extensionPath = folder + "\\extension.xsd";
    var extensionFiles = stageFiles("load-extension", extensionPath);
    var ref = loadFormat(base, extensionFiles, uri, version,
        extensionFiles.Получить(extensionFiles.Количество() - 1));
    record("extension_loaded", true);
    // reference/kd3-cfg/DataProcessors/ВыгрузкаСтруктурыФормата/Ext/ManagerModule.bsl:7-34.
    var parameters = base.NewObject("Структура");
    parameters.Вставить("ВерсияФормата", ref);
    var address = base.ПоместитьВоВременноеХранилище(undefined, base.NewObject("УникальныйИдентификатор"));
    base.Обработки.ВыгрузкаСтруктурыФормата.ВыполнитьВыгрузку(parameters, address);
    var result = base.ПолучитьИзВременногоХранилища(address);
    if (result === undefined || result === null || result.ФлагОшибки) {
        throw new Error("Выгрузка структуры формата КД 3 не выполнена");
    }
    result.РезультатВыгрузки.Записать(folder + "\\returned.xsd");
}

var args = [];
for (var a = 0; a < WScript.Arguments.length; a++) args.push(WScript.Arguments(a));
var exitCode = 0;
try {
    var command = args[0];
    resultPath = command === "selftest" ? args[1] : args[2];
    if (!resultPath) throw new Error("Не задан файл результата");
    record("protocol", 1);
    record("command", command);
    if (command === "selftest") {
        if (args.length !== 3) throw new Error("selftest: результат, строка");
        record("echo", args[2]);
    } else {
        if ((command === "list" && args.length !== 3) ||
            (command === "export" && args.length !== 5) ||
            (command === "roundtrip" && args.length !== 7) ||
            (command === "format-load" && args.length < 9) ||
            (command === "base-export" && args.length < 5)) {
            throw new Error("Неверное число аргументов");
        }
        if (command !== "list" && command !== "export" && command !== "roundtrip" &&
            command !== "format-load" && command !== "base-export") {
            throw new Error("Неизвестная команда");
        }
        if (command === "base-export" && (fromUser !== "" || fromPassword !== "")) {
            user = fromUser;
            password = fromPassword;
        }
        var base = connect(args[1]); // Единственное внешнее соединение на запуск.
        if (command === "base-export") {
            var requested = args.slice(4);
            exportSchemas(base, base.ФабрикаXDTO,
                packageUris(base, base.ФабрикаXDTO, requested), args[3], "");
        } else if (command === "format-load") {
            formatLoad(base, args[3], args[4], args[5], args[6], args[7], args.slice(8));
        } else {
            var all = conversions(base);
            if (command === "list") {
                for (var c = 0; c < all.length; c++) describe(base, all[c].ref, c);
                record("conversion_count", all.length);
            } else if (command === "export") {
                var ref = findConversion(base, all, args[3]);
                exportModule(base, ref, args[4]);
                describe(base, ref, 0);
                record("conversion_count", 1);
            } else {
                roundtrip(base, all, args[3], args[4], args[5], args[6]);
            }
        }
    }
    record("status", "OK");
} catch (error) {
    exitCode = 1;
    record("error", error.message);
    record("status", "ERROR");
    WScript.StdErr.WriteLine("ОШИБКА " + redact(String(error.message)));
}
try {
    if (resultPath) writeUtf8(resultPath, rows.join("\n") + "\n");
} catch (writeError) {
    exitCode = 1;
    WScript.StdErr.WriteLine("ОШИБКА " + redact(String(writeError.message)));
}
WScript.Quit(exitCode);
