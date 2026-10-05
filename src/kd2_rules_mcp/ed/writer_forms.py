"""Формы генератора КД 3: только интерфейс и редактируемые операторы W1."""

from .writer_model import Formal, Signature, Value


def literal(value: Value) -> str:
    """BSL-литерал; неизвестный текст не считается порождаемой формой."""
    if value.state == "string":
        return '"' + str(value.value).replace('"', '""').replace("\n", "\n|") + '"'
    if value.state == "boolean":
        return "Истина" if value.value else "Ложь"
    if value.state == "number":
        return str(value.value)
    if value.state == "undefined":
        return "Неопределено"
    if value.state == "date":
        if not str(value.value).isdigit():
            raise ValueError("Повреждён литерал даты")
        return "'" + str(value.value) + "'"
    if value.state == "reference":
        return ".".join(value.reference_parts)
    if value.state == "unset":
        return ""
    raise ValueError("Непрозрачное значение нельзя порождать")


def formal(parameter: Formal) -> str:
    return (
        ("Знач " if parameter.by_value else "")
        + (parameter.name or "")
        + (" = " + literal(parameter.default) if parameter.default.state != "unset" else "")
    )


def routine_open(name: str, signature: Signature) -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
    # ШаблоныТекстовМодулей/Ext/Template.txt:19,38,99,116,137,144,151–173.
    kind = "Функция" if signature.routine_kind == "function" else "Процедура"
    return f"{kind} {name}({', '.join(formal(p) for p in signature.parameters)})" + (
        " Экспорт" if signature.exported else ""
    )


def routine_close(signature: Signature) -> str:
    # Там же:11,30,59,72.
    return "КонецФункции" if signature.routine_kind == "function" else "КонецПроцедуры"


def version(interface: int) -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
    # ШаблоныТекстовМодулей/Ext/Template.txt:9–11.
    return (
        f'Функция ВерсияФорматаМенеджераОбмена() Экспорт\n\tВозврат "{interface}";\nКонецФункции\n'
    )


def property_call(arguments: tuple[str, ...]) -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2320–2380.
    return "ДобавитьПКС(" + ", ".join(arguments) + ");"


def search(fields: tuple[str, ...]) -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2555.
    return (
        "ПравилоКонвертации.ПоляПоиска.Добавить("
        + literal(Value("string", ",".join(fields)))
        + ");"
    )


def condition(direction: str, branch: str = "if") -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2689–2718,2760–2805.
    keyword = "ИначеЕсли" if branch == "elseif" else "Если"
    name = "Отправка" if direction == "send" else "Получение"
    return f'{keyword} НаправлениеОбмена = "{name}" Тогда'


def assignment_width(directions: tuple[str, ...], field: str) -> int:
    """Ширина фиксирована макетом направления, а не списком оставшихся полей."""
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2236–2242;
    # Templates/ШаблоныТекстовМодулей/Ext/Template.txt:44–56,64–68.
    if any(d in ("receive", "both") for d in directions):
        return 53
    return 47 if field == "group_flag" else 36


def dispatcher(
    name: str, function: bool = False, cases: tuple[tuple[str, tuple[str, ...]], ...] = ()
) -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
    # ШаблоныТекстовМодулей/Ext/Template.txt:137,144;
    # Ext/ObjectModule.bsl:3080–3125: пустой диспетчер не содержит Если/Иначе.
    kind = "Функция" if function else "Процедура"
    argument = "ИмяФункции" if function else "ИмяПроцедуры"
    closing = "КонецФункции" if function else "КонецПроцедуры"
    result = f"{kind} {name}({argument}, Параметры) Экспорт\n"
    for n, (handler, parameters) in enumerate(cases):
        keyword = "Если" if n == 0 else "ИначеЕсли"
        result += f'\t{keyword} {argument} = "{handler}" Тогда \n'
        result += "\t\t" + ("Возврат " if function else "") + handler + "(\n"
        result += "\t\t\t" + ", ".join(parameters) + ");\n"
    if cases:
        result += "\tКонецЕсли;\n"
    return result + closing + "\n"


def empty_module(interface: int = 2, title: str = "Manager") -> str:
    """Минимальный состав пилота; интерфейсы 1/3 обменом не проверены."""
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
    # ШаблоныТекстовМодулей/Ext/Template.txt:1–11,19,38,99,116,137–173,190–199;
    # Ext/ObjectModule.bsl:2725–2747 — отличия сигнатур интерфейса 3.
    pko_parameters = (
        "КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки = Ложь"
        if interface == 3
        else "НаправлениеОбмена, ПравилаКонвертации"
    )
    pkpd_parameters = (
        "КомпонентыОбмена, ПравилаКонвертации"
        if interface == 3
        else "НаправлениеОбмена, ПравилаКонвертации"
    )
    # Ext/ObjectModule.bsl:165–188,2814–2839,2869–2878 — порядок областей целого модуля.
    result = (
        f"// Менеджер обмена через универсальный формат ({title})\n\n"
        "#Область СлужебныйПрограммныйИнтерфейс\n\n" + "#Область ПроцедурыКонвертации\n\n"
    )
    for name in ("ПередКонвертацией", "ПослеКонвертации", "ПередОтложеннымЗаполнением"):
        result += f"Процедура {name}(КомпонентыОбмена) Экспорт\nКонецПроцедуры\n\n"
    result += dispatcher("ВыполнитьПроцедуруМодуляМенеджера") + "\n"
    result += dispatcher("ВыполнитьФункциюМодуляМенеджера", True) + "\n"
    result += "#КонецОбласти\n\n" + version(interface) + "\n"
    for name, parameters in (
        ("ЗаполнитьПравилаОбработкиДанных", "НаправлениеОбмена, ПравилаОбработкиДанных"),
        ("ЗаполнитьПравилаКонвертацииОбъектов", pko_parameters),
        ("ЗаполнитьПравилаКонвертацииПредопределенныхДанных", pkpd_parameters),
        ("ЗаполнитьПараметрыКонвертации", "ПараметрыКонвертации"),
    ):
        body = (
            "\tНаправлениеОбмена = КомпонентыОбмена.НаправлениеОбмена;\n"
            "\tВерсияФорматаОбмена = КомпонентыОбмена.ВерсияФорматаОбмена;\n"
            if interface == 3 and name == "ЗаполнитьПравилаКонвертацииОбъектов"
            else ""
        )
        result += f"Процедура {name}({parameters}) Экспорт\n{body}КонецПроцедуры\n\n"
    result += "#КонецОбласти\n\n#Область СлужебныеПроцедурыИФункции\n\n"
    result += "#Область ПОД\n\n#КонецОбласти\n\n#Область ПКО\n\n#КонецОбласти\n\n"
    result += HELPER + "\n#Область ОбработчикиКонвертации\n\n#КонецОбласти\n\n#КонецОбласти\n"
    return result


# reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
# ШаблоныТекстовМодулей/Ext/Template.txt:190–199.
HELPER = (
    "Процедура ДобавитьПКС(РодительПКС, СвойствоКонфигурации, СвойствоФормата, "
    "ИспользуетсяАлгоритмКонвертации = 0, \n"
    '\t\tПравилоКонвертацииСвойства = "", ПространствоИмен = "")\n'
    "\tНоваяСтрока                                 = РодительПКС.Добавить();\n"
    "\tНоваяСтрока.СвойствоКонфигурации            = СвойствоКонфигурации;\n"
    "\tНоваяСтрока.СвойствоФормата                 = СвойствоФормата;\n"
    "\tНоваяСтрока.ИспользуетсяАлгоритмКонвертации = "
    "?(ИспользуетсяАлгоритмКонвертации = 0, Ложь, Истина);\n"
    "\tНоваяСтрока.ПравилоКонвертацииСвойства      = ПравилоКонвертацииСвойства;\n"
    "\tНоваяСтрока.ПространствоИмен                = ПространствоИмен;\n"
    "КонецПроцедуры\n"
)


# reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2300–2304.
HEADERS_GUARD = "Если ТолькоЗаголовки Тогда\n\tВозврат;\nКонецЕсли;"
