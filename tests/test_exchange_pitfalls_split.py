"""Дословность переноса разделов `kd2-exchange-pitfalls` в справочники.

Хеши посчитаны по `git show main:.claude/skills/kd2-exchange-pitfalls/SKILL.md`:
sha256 строки без перевода строки, кодировка utf-8. В множество не входят пустые
строки, 28 строк заголовков (начинаются с `#`: при переносе меняется
уровень), строки `CHANGED_INTERNAL_LINKS` — внутренние ссылки, поправленные на
файл справочника, — и строка `description` шапки: #66 сократил её до 400 знаков
(бюджет описаний — `tests/test_skills.py`).
"""

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_packs  # noqa: E402 — скрипт из scripts/, не пакет

SKILL = ROOT / ".claude" / "skills" / "kd2-exchange-pitfalls"
SKILL_MD_LIMIT = 12_000  # общий бюджет скиллов — tests/test_skills.py


CHANGED_INTERNAL_LINKS: dict[str, str] = {
    (
        "  перебор кончается без поиска, и после цикла — «не найден», шаг 7 `search.md` (стр. "
        "9050–9063). Поиск по всем свойствам"
    ): (
        "короткое `search.md` после переноса в одноимённый файл указывало бы на него, а не на шаг "
        "7 справочника kd2-rules-build"
    ),
    (
        "Исправление до проверки вносится шагом 6 «Как разбирать жалобу»: правила конвертации — "
        "сервером, не правкой XML"
    ): (
        "раздел «Как разбирать жалобу» остался в SKILL.md, из справочника на него нужна явная "
        "ссылка"
    ),
    (
        "- **Симптом:** объект удалён в источнике (или перестал проходить отбор регистрации — "
        "раздел «Регистрация»), в"
    ): "раздел «Регистрация» перенесён в `references/registration.md`",
    (
        "  приёмник (шаг 1 «Как разбирать жалобу»). Исключение — обмен через внешнее соединение "
        "(вывод, `handlers.md`)."
    ): "шаг 1 «Как разбирать жалобу» остался в SKILL.md",
    "  («Как проверить исправление живым обменом»).": (
        "раздел перенесён в `references/exchange-check.md`"
    ),
}

OLD_LINE_HASHES: frozenset[str] = frozenset(
    {
        "0056a20c64357a9fcd459d67deeda6b72bc1db84a7807595f177ba7003985d3d",
        "009598761c1a39d4e14aa1c5bb31cd9f913d99b6a547d8ff270afeea3c954dec",
        "03ee072ecff6f864d2d2304877ffb72e3a06610b2cb0b864e1dd9073f1733320",
        "06685903ef050992c8d98bf35c8d93053fc0ffe619343798af1ba23ad6f0d05e",
        "07233b02cd24b97accd5afb9db32bcfc5e1dad3362bf413875229cd46176e6ec",
        "09bfd47b7f3cedea41fe9e68f81dce773d870053a0f4658d18d29aa6e488105f",
        "0a662318c01acaa5d092c1078f1c35f6a8ed6350449d796d371ed3fcb1250ab7",
        "0af960967ae3df27f22fe07743d75a50041df898f029f382df15a6872f64a1da",
        "0b035a65cb9549ce672a12da758efa08d082a043489baed2fec84c3e48cdbe6b",
        "0c035361e40c18e64732fd24f8619886c8894fe0164ef977736bab9a2723119c",
        "0c33e4c4c34d4b5f96537c4614acdd955cf8e97aa6519eaed176b2515f610f75",
        "0d7290749dc811de3603b40204d0ee5bb0787fd23017adf4d39e6bc3f8403fa4",
        "0ec1a612f8aed5000138fe800336fa4d52c706241c6bea5a780a213a1fe1cf1e",
        "0ef624547d51ac75f305d2d18c317e29a2fe9e4cfd3c8275696e77896c4c40c3",
        "0fa98478811c5fd6b334711b93d7f8b418fb8d725387508cb9e091a383748c86",
        "1003d732120ef2fb39b87f8b92a0344bcc4ba0da9942a450b9d2635f34a37864",
        "10cdb8eb922182aa4ee4e0cea8885f17616a27eb7946aa1bdc20ca0a541102f9",
        "10e9c70088fbf24130a77b5ee91e5c707b57961ca178feafd6a2f15c365a198a",
        "11d9e6134b46aac1835fc3e3eee8fd9848690a4776999f0e04c48a3f7e030b15",
        "12322accb47641179bedb95b9e5b03b6a9e77a65ebf6001f7fef3f495a5a9414",
        "12593f80e22b78b9e7a8f7c618f03f19686ea9bf35e3387882e301446c391afa",
        "12c58540ca16029527c72ed9ea6e841329bf4cc8438fbac5fca480317fb4faf7",
        "1436a22c876227b1b939a6044813c4ef30c5882c4c4f5402fc73b61ed701379e",
        "14ce4c51b6c7126d47bd06ab66cb80a60e7e6431291bf6a1f6d8d0f5414e2332",
        "16d5ca51944a288f0302ec2f69eb253383cf0c7740489cfe4e747eee56a601d3",
        "191982b11788e724fadec77489c6a98b95967b0341f39e857a1a5e27f7e8ef2a",
        "193046e6a9657ed18a962f263b2cf000a69e34d2c32489e561615bd9190c498c",
        "1db64b25db76415b1df0da56dac7f090a00e8bce6fead932537ccd76cc8faa6e",
        "1de9da7feb8f0f7383c708365c5bbc7bc8c66c7090a309bc9e002428a7039b37",
        "1e09b722d78a1f6262d0663238460bd3b7de9ebbbfdbcab84eb83754f21b9691",
        "1f78c651c974a9a3432f93d6615fb0307a4d8de5a0c2c69ec5a2699453a92b79",
        "200024cff08bd5dd13144053adb75d630eb2eb55ed1f2d981fb21b66794828b7",
        "20a02f96ab13b82199cec5911418e476a547754879755c2dc46ab5556cba3316",
        "2186655a702c9920b78cde56f9aad645d78d23416d8a5ea1fcd0e770b2600139",
        "21ce6fdec591f18030407e40f6bd617261e3fc6adcd84f0656c79e697452790b",
        "223618ff1cef1ba4f887302114f17ba2bce42698ebb2fce4e24d88f2f8a3c966",
        "23cca02b74f386463efa24796c7a9011b0d2c93f0c38b4d15d5d276205b59b43",
        "23edcb89ce433333779f172872c394e8cc2e8195764db256b98e22c984ac5eba",
        "2430d027174dbad93ae421311e5e13668f4d49b4ae47a1d54a11bf5e872cc195",
        "245bde15510e8b53579f8dd81a0ad5d51e9d95b02b17842d5878d4e4e5ac3726",
        "2657bc6c0dba5651d5f385ee10261451fca9036333b9592dd36919e50e28868a",
        "281354daa75a47a3b0514f437463d23e8cf499fc5a7ace22215cca92776d3fe5",
        "2832aa2c4cafe5299f2ab0c9b09c10e79629a22b167edd06d07b19a681b20f09",
        "2877a7126512857f939d9328d8c0bbac0d1a0dda7098c62b4061680f831c3d61",
        "28b43d31cc6325814df9a0024474db872a67902273bd0e9c0afb35e79468fade",
        "2e69709bd315f9694a07f7979f2c98ff52f01b1443e1dca5e3b0b02610d1bd36",
        "2eb21e8b8514351eff94f765c0c960bfc388eb5054693842cbc82ea1226d446f",
        "2f64e23628bca78a174847a49e6984f7bcb9bed3cea5096e2a61963832c11181",
        "2fb3aa747d8cb65afe87a6b4071a4d5027a632761053a268a1b8cc6f38cbf854",
        "305ebfe04b95800a0c051c10740bc75e6fce1502580ebe03b08e45d371f204ee",
        "312b2c70070f69d9d5a536dd3e3b33a59b2c6e202dca07ed85d2e42697e4eca0",
        "31b44bf99be6d52a2f6c666abf1bd06cc8fb287efc920021ed8b869c243f6eab",
        "32aa79a2a065017fcb67b284fe4b83ddb489494abdebf454f53613fb39686098",
        "32b4939e46923e6e548503864cfce6b1635be8f6c88822c449042628fa89585b",
        "32eb128542c013f4840fc9846b6cf64c757e8d5b2667f60301d0f918968ff9f4",
        "3539071a43eb9e140ff7829c8cb17ddc701ae4057f02342ca09ebe66a4c91787",
        "363c257dbb2b8ca1062f834c5e6fd53e80e42d3854d1577ec8b28375da0e6ed7",
        "36a5a1a3cb76eeac7f37590fb88d07a0030d57a80383064f91f091c4605518b8",
        "36f6b24cb7fe5d87eac41a973571ef2e3e266c3e779e03996443910ec313be20",
        "37e4a7240b5b97c8c18ffa207d7e4e3a22d8d177377518050de029be139b35bf",
        "383639affc33a998c9a143788b62d961a32ab090dc7a8fd7512b542fd83e25e8",
        "3850253b577ba143ca7b2f271553c3ff90d19601ddb8172404627cc209f4e1c5",
        "3985beba8c7afe8c3ff517765cc212e885c6d509889382a0e73c0c571b2c2ccf",
        "398eea7f52f6378f9d2dbda1e6ba7447c4e74c53ab664cf7d0aa168d767f0989",
        "3b03c4842d36463be9ca8d2b0facac5f00b91b8e4b006d072f6ea02602147958",
        "3bc87cfe343668f5589682e743004dee8437b242b62292fcd897a472b595aabc",
        "3da18d08aa9e69b031c1eb476830e7d1b5473748e66a8bd4ecdbf9a384fdfbf2",
        "3dbf39f22a6250be64789f9a012abeabf50ca615a188f409061d214d1a6ba27e",
        "3df37b60213155d46212cd90a890f2d1b23235865f83ab9151550118508fba6c",
        "400ca21f998b7e4dd4df9ee78eb53bdc9bb14c1c95d68b465ae6eb0526ade8b0",
        "41381a3a66ce6f51a57d0408681cee63b49e89878d851b9b42690849a4d95715",
        "4148e249fd51314b027f355fde41596e38cff72535458c66fd40b52065a1e9c8",
        "41cba0e9133c9674e4a588209f90353f4cb34bcd27501cae773d88f8380da9c5",
        "4219423d093e24051da9c4745739ba98d0cf000206e2d07dbab231dcc7d98634",
        "4227e8aa8764fed4593c5a61d329e34abf6b04f110ec83bd8e0eda25b223e622",
        "4527991bce85ba0834e517e6cfab561aaf74b0c3e6c16e5bed5073f74784face",
        "492199511ecf063ff6cda7c9c3abad73f7ba9ff44a8e9ec91f48287335eb7196",
        "4b26eac86250bc6131155f7c91cb2aedcdf9df3a39165b9f137c519daec07cec",
        "4ca7d09720b9c020cc6af13a2b39f25e1d89fb5620b8f3a62262c1f49bb09846",
        "4cd3f5e4ab223c327cc57130ff69920ebec44ed557d96577bdba2fceed6f3c22",
        "4d7d841abfbaeb95383ecf81563adb7409d0d65a5d316b6b83e2b2f902c38417",
        "5337c855022963522a59796217c0c44a3d99188210679cc53df84e77abffa301",
        "5603e50ae04cc4191e8f19b5ab415aa77f2a590e03e0396a21ec7a27da46a31a",
        "57235508984bc9ffa33a1872d459844f8cfdf576835c37f421516babd64345b1",
        "57755cc5fb98ada7fce78d23f8eff0bff7ce9b0a7047bca4f53df5976cd287c9",
        "57a1ff74ed43713ed21eb5e530ea7a5886b78c609b844f83ac14ed91fd085a08",
        "583ba8904487dda8e0fed7c93d554bd5c687cb0b528d33164ca63a4989481716",
        "5860981313a2dc4bf17006bb076b31fb415522d64b674444630d1da07661ef3d",
        "589b2c0e04722d4075404dffbd15302460643b31af5a431305e89c7295afc2bf",
        "58fa13f69970b825e2e084b3e2bd98bcfe86b1a8c41a24fef2ec97e8710b45d3",
        "590e54c15cff786793139576c614d1b6c2ad89d7c50a06ff9852d4ff1f24806e",
        "593b494a3baa9aae356aabe0cd7b4367222f181711e65ac9211a7db10bc9e80b",
        "597b48587f92b2ed32db0573762126d04d9ae018bc218c5503607488f9f30bb6",
        "5ba8316b04c65358268135c7e15c5a81409e93d06398f8e3a78bc2d8604e4d14",
        "5c133f4cfffca4121ee2bd2cb4b38067825a24be56ab07870778cdeaf6ec7b83",
        "5d8bf1771d8c9b5653c31f9307a4c592494973628f88abd6a5786d9102317eaa",
        "5dfe9e1b7bd2e83ea9fb3c3e9de4eab549ec8c2cfaa1a0e8a2e183b6756da8be",
        "5e36cfb4c440ab20212fff9247a7b4b2d4eedaaa2d148a1320cd92994d092459",
        "5e668c5c78c42e6dfdba5ab6dae59c7b786983528f073a50ab8a8782bc567ee6",
        "60d788558a81f3ef4fc6b2660382022d1c654ff2bf4075b8d94ff620898c77ff",
        "60fe7fbbf1dd348771d6fd482ba27eab451bfbe82050487dc75822d256d322f2",
        "6217fd93c649426c75b3df78788502ace7ce75c0a2625dfba420d905ab7c4d5f",
        "624778d91c89be9760685b3feeed51d2c5e6022d2661839a2118bb18811190b4",
        "62fa15d0e15ec8f7dce499d206307c536b8c39fd2aef60dabe4103820bb11a6d",
        "643056c9c884de9020995001bb921fba6b773e984959715e368e8e3ebb86e6e9",
        "6437b72921ed765aa382b6773dec7f758f6981f977c94cabcfac6e9d36257e08",
        "648908fddd83c2ddf9268ec6a916c1ca9c3754cbaf6ca7b18e26cc9cace92df3",
        "65f8b0366bd8e6f41edf2a1813a41108d9e898781d30c233d08781368a18ec40",
        "65fcd5877c66425c1de8a127ddefcfec8333c511203b26f378fc99c4ece9249b",
        "679de2a4a8b640d5374df45ace0c91de30abe8d0daf2482405e182e1977b3217",
        "68e623e4e954bbd5ed73952f3a28ddb6e7095d650ffc3505927be4bbe4873ecb",
        "6961655749e83ee4809eed130b5c5cd39c4b1840210564b3caffb5aad6db9cce",
        "6a84be803fb656fcb785a361ae0f6538a9db8c6a59791403cfb0268c3885dd0a",
        "6aa740458b377f2eed151d242ca394fc422ade3e65f7f31d8d6f74a4b26f7a60",
        "6abe911d9b23a693a90d31f5fe14db542f746e2990f15f645a00662205ab4115",
        "6b88c2d4942701f58739881d8806ff1bd03c2ba7eb4a9848733d457377121217",
        "6dbfb93d57a9e87461fe10da97311deaddeeb5435927fe71386ac7a8e450297c",
        "6fa783d347ae6cef9874d5b4501dc5170c2e6d93d2bbc41af44dfed4b5538ca7",
        "703f77b9e5eb439dd5ff7fd1e0d208e905e5dcb39e1dc442ed76b11828b9e982",
        "70b453ee03322e10ae9a5e4ad592b0aa7589c78b18fdae16b6b97aef5e83d3d0",
        "7124d77d386215fc3bd8dfe2205db5198e676efb4b4abb48dff09d69802c9e86",
        "724a34440d75f1c45d7ecf54c7aaf50fde32b16f82f6a1c20da6a4b8b8192f36",
        "725cc5d569e211f62f9bc05a8494f8b339720c06f507a098d6f57e7ba175de7b",
        "73ad2cbea94169e989918909925efd3233e11bd4ab610f5b0ec28170335f9a21",
        "747bd5c1534bcfd22b22b047fd571936fc429753aff443b2e555a8bb10ab3df7",
        "757663b20b8596f07faa923f1ad86d26059eef2f2c1f6ae4ed7d77e8a21f3c03",
        "75f1e55b64634031d81bc55157e643b41468a17a71548035ab7d3631c9e9d6c3",
        "79dbe848592dd6546c32e1fe42fb3b8c3bf6a3c4528820bde941118b30c34018",
        "79e32e37af40ed93f77ea40e7621c562fbce884ced254531411c26e71570f49d",
        "7b6a9d99f6b7aaa4168dd68cc0460ea9c63eb4ddf66ab3e48c15a20ffda784e8",
        "7ce4eabb9851d787e1f4ebbea29e707feba524b1b990a6dadf405f0fb2d73632",
        "7dee85159cf650a495c0df654dedcda6f838d097e12c968515e952906b703f4b",
        "7f4c10f52fa7d909d4cf47533eeab80b01b1c2c6b585b7fc974001c58d76212a",
        "7f79a6da07352efd6f6fe2a667101061e8dab36cd67ad0cdbc637ce642633c89",
        "7fb69f50e0c204a6cc498431c6682ca78db40562a06404cdc962fdbc4fb9efc6",
        "80394145d4bfb3f00b4def8ef6194ef446d25ee6afb897035484ff385c15ea7d",
        "80f7d7adea748361dfbbb2b65521d2cdbe1e4bdf24d37b7bac65222fc035458f",
        "811603be1f9602207c500c548128aec7454872f527fedc01127ab3493eaa1a51",
        "8131b39c50904375ec59c8d2044cf3984955db1bbfd33c2fa8b130ba24a368cd",
        "81f7d6c680b5b0f38fa12650cccc5c6a133c834558d1f032f1859dfffe502fae",
        "82ffd2ac47207fe544e38971bcfe87a2ede42de45bacf6fe74ccdf5507d10a8c",
        "834bbd311eb60f3bf6401321ceabacdb2c0e4931ec18aaaaf59a7243e1d82fe2",
        "84d7242962de852232c2f71dc344cebe3b2d4c760bb8384f675bcff65f94249e",
        "84d86dd464deecbda23b92abf4999c4519cba30e9fab4c97a543e776975439ee",
        "874f6dcc2a85823a13a32015282428d04bb29fc09aa9302d7791250aeb6b5d10",
        "88b46b002b16a9e70f916bea6b6de3db7993760fa647c9c76b0b19f1fb7b6a80",
        "89afd6efa42973ad6197322164f8eea83676365eb4bd2a196d75094603dd949f",
        "8bae592573ec862d9b0f484efbeaa31da145e5b3f72528c2da0b8311b5f4b852",
        "8c15971603a8bb9ccac5281967baeebf0782a0b16e73be5d92d1f7475d056262",
        "8d25469e72da907bc1150e04e918f617a966ad328ba790dd2360e149ed93d422",
        "8d9c887714938da5ea55af4dc0bb939d72cb6830065e6ca68a21c649c9b7b673",
        "8e525d73a5360619594c3975153be30a7f6df3f1e6e2e40c040f0bf52d1c33a5",
        "8f641975f99ab5489ced845a29e4290d18866e2d719aae71981f6db652a98097",
        "9077a82edb382ad89813b03a7969da0d7d06313f567e5f6c926f7bc9afd04fd8",
        "93686a54a0a194056032fec5bbb5c40d8bde185dbb1b29775ec7e6b81bfb7e93",
        "938e3edc3d19600da0f84c54c065afe2067de8f300307a8d7d1fb2dd1a454b0d",
        "94e45d10907626b69c64910f6426c3db779c06117da3643ceae2f79a99c5768f",
        "951a23c031c8effdae17d1058af20e50b5a06c180725e10292b342cce157242d",
        "95ad39a9b819b0371f381816a66de1f5edf97648ccf83cf1115b50ac83f2dc3b",
        "960173ad40e22e375d58a631c78f3301f26edab67bcc2b67a5cb8c29979d3834",
        "970d37fa94f0fc979ed4da4ecfa11d8b7dba44777398732c45171f079e2401e9",
        "9814c78fe27c2be72dbf5fa6630eef540cfa4dfdb7d77bf096ed7fc2b06d0f2d",
        "993434ef57a1b59e8c4c6acec13c09f2cf8725678a66ecbc41e20616da654f10",
        "99634cadf03cc5c37090afce85c2f7044716ea893fe15e8afe482dde530bd296",
        "9a2a5352335b27eab3d43ec94920cc27c69112cbdda36901a558dc30d36f197d",
        "9a50fde0884fbd667f34676230a8a41ddef0e105e0c177f7d1331fe0bb2a9c8c",
        "9ab1af74b145d7200f75427cf589ba2496bfef7fcd0a5f2e2512ab84b38caa97",
        "9b1dc0bfc95e834474e0de34f78e42c4d20f0a6cb06b00743fbcf53ddcce7b34",
        "9cce7882685cabc8e8bc82bce459fe210bfe04bb6fe36d2f24875633e5c97207",
        "9e506597f29fc803e40b4d0e6cb3f012782a75de4bca91cc5c85a85ee1bd9f9f",
        "9eded22c8edfe3746cf2aa18b74fe620d447b309583a9af4b59b5c6c32e3dcbc",
        "9eef66695bed0b3089023ca7e52995391e5b9db2dc2ed41f9238c366d7b8be12",
        "9f918787f71fcd9b409f895a6adeab11ee40bae30efbc01db95a2b5fff0665d3",
        "9fa61e02091aa202273d551f8239b89e41fd1906648d687672194f87bcd2731b",
        "a014da174fef15f70f1fa4fa6495aaf8ad3f3912d898edaccab440c33b425c9b",
        "a0240c83522b3593d806467a19788175f4c26526d2f920b1b324d78a0cce4618",
        "a1eca10ea6fe02184d2344091c6fa8a398d0a6d602939039735d87b1111cf50d",
        "a21ff46164d44a7af9c5716cc0df4ecbb1766760cdd2361187a51d6b22f0676f",
        "a28a9a30deb0c38eca5dd36b3194d3510316cf58081cdf62e4731012f285a9a1",
        "a37f9164022e87df0f179e9e59cc5a5897d137ca678f2222347491b83ae197fd",
        "a52ef3d90e6113359d738823d7c4b0542e23e16514ec926eadc2177dceb7ef2d",
        "a6594dbfdfd8593e33b2365aa04223518bb2a1e0b95d7371c6a6bf2bcb303826",
        "a6a8e2639b3f818cc79c2ca27ce505579affe8defe586bae8fe6865e43385658",
        "a8d9137d3072b2ce74dfb93db5de7610402e468a22eeb2b3d66c3a9b0ed0e690",
        "aa51052a0cf1b189d99c2d4e5742838a5d6c9c4ac4cd0d068a5128a483378cb3",
        "aaa86a2632cf301f60577cb24dce9d987e9c62c1d4000d3f9b30cab0f65f6eb9",
        "ab1d08c0fd340e6352ac5760713b27c3314ce9775df9a8ecda1c1f289fbe76d3",
        "ab4fa6b4564df253249cfcf9da6a8d5d46c156dafbf4ad66827fbf79e1c9659c",
        "abc46d5a006cd0fde5aed6a14f74aed9192ec2878c76a224ae7bc1dde8ba2481",
        "ac207c5c3693c048c16eb153f183a4cda60a7fbf5d14db2961517642d60b8427",
        "ac351ccab1c9833768fe3ba96e0be1907cdc8980ce43901046e54ef4d2a2d834",
        "ad6eb549f1d056597a901fd787d28a809b811b804afb46319ebd119d78b6cef3",
        "ad895554e2b6ca93a6947d3ea9c79fe188e80388dd7646163d78441a6891dcca",
        "ade1630c4d682fc705499694f88892b8f01f02e8ee077c4fa87b317158c1faf4",
        "aee2169dee8a48d73ae7c119da1c312c2a6b316869a5c52b321689a2b54a589c",
        "af12fe7b69886a596502f54338be50fe849b76c1dbad50a9d9b2a5709c7e190e",
        "afe1a64a1a7c04ca368129e590ccf29b942e69846b8f002a4ce20076c69a16a2",
        "b0ee4ce0b7bec41bf4be2c0163e07eae345beab346e54b6faa4787ce88a95d33",
        "b1cf1f56a8c938ef498af07c80536703a33702df6906d6b447c6ea056e402a2d",
        "b5a1b560e5dfd4987aa5f17e29c1a35f8438941665b27ea772527208e3a18376",
        "b9d2e7e062b6e829fa0d86cbfdc8ae05e142706f99f7c95a2f02b2a8345a0f80",
        "ba1d43fa1a17f601a3ec9f26d52166edf87954b7bdb46e580d274718eb4d13f0",
        "ba8182012d2b33fdc4f417a22719079c8372e5e366c128a1eda068c9f2acfc88",
        "bb7688a412232ca42027a2b6324842e1c5fc05a8dac88069cdb17f48d643619f",
        "bbbec3e67c0f896b5b2aea2ccce1d2d2edcdc16f37560eb97f31e044ca8c295e",
        "bcc86d6d20cbdc9f9db87670d159acd166586da00442cdbc44b087d761d56b6c",
        "bf1bb7358b15db281e5650680ea5d8cdd34a131230006ced7570c892bfc9af67",
        "c00b9cf4a98d98f2d99d7b9806d1804de06d83bb7ae9c56cd5d42d88bc35fe58",
        "c028a86b20a95b89f2b24e11079a8564f5aaa28f674843004ed618159b4260dd",
        "c15e805168c130d74b95070701e4d29f9abdac0be31a772743e70d6aff50eb62",
        "c1a4d8c5c0902b52a320808ff82750214b21cb0c94fce5e917676ba71b526fdf",
        "c2cd7e1952a59305ab39022dfcf041b924b103ada6f84ae03e4040435a111708",
        "c38448842df5b9f0c25fab24af577cee5aa670af882469a41c72b26731ae58b6",
        "c4a5bf02241534d3d72b530fedc7ba14f9e406d57f6bc14404aca5e8191ff6c1",
        "c50cb9a0c1db11ddb0ce12519946c71c42e159f67e5f996a306e2147197f7d7b",
        "c534cb20435cd96f9275fc4ad4a708c08eba3fc96e55e29aa8c88707da3abf67",
        "c689b4f11678f6e9bf862bc1ad2fb4e2e1a191e51a00b961b69e87e1c9676062",
        "c7ef6afc60a6f3191681f3fc3a03e8c066a55748cd9a117ad4e5c44c34272cda",
        "c82e4fb1cd813b76d4be59aaf6de722c5ee9ad6cceb49798e3c49187496c96e2",
        "c86012398858a207413aaffacad8734c7b6f06f1e514b3e8ba39608b0fe1cbf4",
        "ca0eee72a4437685b3ebdc2b34b2f5cb7772a47918d2632c1b6d0b59c2a76538",
        "ca340351a7090f4b93caa86a9ffebabd79de9b31a4e3e3d7a2ed496c2587c398",
        "cadeefae59d00875f578709b21b25c8ccbf4dd881f5bc879d7cb166b8fca5681",
        "caff4c9aee460cbf5928438e127ffc696501d02fe1b4a69d17d7d480b6d44bcf",
        "cb20bb68cb8b36a96393971953530122de757ced306051e3041364df460b33b1",
        "cb3f91d54eee30e53e35b2b99905f70f169ed549fd78909d3dac2defc9ed8d3b",
        "cc05063ae04521b253e74d4bb042a80e39f9933cda3474521dd21f53f267fb8d",
        "cd4fe3bf03e0561d6da328c80e1c14cb7a0d186f8b6c2c5d9384a2ddf13a502d",
        "cf0171d9d1f85ab6e075dcd0b2d90c9a5d61bfc5b37442ed80f14acfe49d4519",
        "cf3d71f92749fda369866731f4f136e12ebdd11751971cf0dfd8bb787a8a1fd4",
        "cf930ca69293a8e14daac91875fc2e875b864dce300b3f8ce67fc74c7d87f947",
        "d0583605b8391465568e429647c3bcdc1c9630b9fe60d11faa2d5460f224aa2a",
        "d130c3006a75e31cfd0a5cbdd8dcc0888c0e38ab77127144ed66d166fac27518",
        "d276b5daa913123199f7677181eec121ec4efb16256d3e9558a4894971ed02bd",
        "d277f0327d7f887a4ada8ef33fee047255bbd3118b2c62d660713210bfbcea3f",
        "d3c0ced917972b89be43cfc53e1e1e1fd098f7bd91e8ed03413f6b39816df013",
        "d3f57fb0ab1f77cfa77d3fb724e91db88c9750ae76ac0c4ce21e8601e05564f0",
        "d401c4bc47d82bfee3010caf2c95550594764dac506cfad39c3aca3c6ad19809",
        "d41fd9d71442cc55065c82cb2b600d74c63ec68208d08fc6f1ef2da15b5453a0",
        "d5418cb3717cd763d8c19fe4acff1570de37df38139eecd2551f41c1c69816ce",
        "d76858bc3b462920a87431af36a4c5995424531bd4989eac9c8308f3a0eb368f",
        "d8cac1524b295e7ce81b81440f0e242d6e83273c07c0b685db7340545826550b",
        "da07f32a6645e951c57e792ae11dc6c121e7c1ab643bc2bbf5c58e8904e51846",
        "dab87517deac8bbff58b3fa1a8018ae29b97f99a7462af5de51bc35f64d25dbd",
        "daf0a3efc70b5df4d765850d500282217f65bed2f9560ac5f45dacc6b937cf3c",
        "daff7a65a58b7d48e46fba676347b96a6a759ef1f868e7765a284c704002cccf",
        "db82116e63e6456d4900f1840387b50a1857af1a94ee26e3fd3734c1bd1f7787",
        "dd307cc8246699957f38d4ba594a733cea6cb5312e71bb68b6cb7d48f7d2b4bb",
        "dd7fdf9e73a13b8c5fad853208cb439429e685891d7921ce6f195cbb9526cf7a",
        "deb4fcca3364dcc4828511ad2b4ff21d9773996158070b1e33d3a0e1ec576b11",
        "df1217d4400681eca04127cf5c067c4ff909ac5d16660d8ddf6cd9b1ead20845",
        "df6a0257327c28194903c8b9ae12170818072be32da6ad2f4797c8c45ef81690",
        "df815b300ee2a0631a6c500187534d9a090a3c0964bfb46a6fbdff9f485a8ac6",
        "e0052671a053f30e0244dc3ebaef94ee5b27f714d60e775ad24dabf547b9f849",
        "e0652ae159d68fe1e66c6ac022b5b16f1fe5aac7fb5ee41e13c26dfa5509c836",
        "e26e26309280b787f20617f82a67562e452453841e527502674c2a867104f819",
        "e3203caa61f9cf2ac6437f5325de64bac78616e97b8a38e836bc3fef7281c3e9",
        "e3f30cca9a1fe8aa3818593aaac48c96fc8bbe40fd85e69637cf3a107b192b6f",
        "e4937537e667cd1d3e3454e7e78098af3270840b23b91854f40d9da347f8273e",
        "e861110fd4aac697dc30cc67f56f9e2665516917e54ed1f91a8f9cc95e6d4e09",
        "e9f9464333fa04602ec9a90b61740e21575d79d91c7422c9ee83b669682b54a7",
        "ea04a57db07ed53e5af4eadd5678eb56b2bc2d5a92546c78fd52ffe54e77b5c7",
        "eb720606c13b2ebd79f0c424c033ed8aa6538eafd42c5bba4dfb6e7a5a4c6d3b",
        "ec662067b51f54187ae2c2e26767f45ce55df21830df3e289a28403ef7384eb8",
        "ef3372dd19138e2eadbd4ec2ae825df1542961d4eff358737188113bb19c8c74",
        "f092b18a35197e394c6d7f563372246652d598b169dd1f41ed7651c9d70169f7",
        "f1ad98e91485f0db2509b09d1fbcce4e527d6619a9999e70c4a1254b90630733",
        "f1be9398a0f16b7abddd45fbe95b0fa8a645370ec8e53cfa6460e5f8e3101d55",
        "f2508a2c2feef0506d136a41f8fb44aeb20a059d5be3f1b9662556edf8df468e",
        "f28c3c06128b2ef9b0095caba2c9b203433cd41d7ee8bae1dcb5d297cfa48ec4",
        "f2ea866cd72d27d3382abbc8d29a6030b2b8d39bc5aba75cd2cbfb46e463a06e",
        "f4bf0355c124839cd901129ecbda07f9d37dc7eb2e12edce9ab6a5b1833057b1",
        "f584e291dbce171e181df34a7d403940a4bf48dc1ec97538dd6cca1b1ad813d9",
        "f636efbe8fae4cc99113a43b3a6c1254d6cf45b35dae0363243a7377b6c8d36a",
        "f640e08fed1627190e1ff951f9dcb838d05d9cc93a079380f5cbfaa1030d2589",
        "f8d4407fe75c9ddf94f6bb4baa8ff682d64900bb2324481ece8e40a67248ade6",
        "fb45cc4770b2433571e80bb11bf6bd791f529e1db4e96ec927d70bbc082f8dfd",
        "fbfaa0a373a21404576ad7dfed2eeaeaadb40b83d4516ada59904a28982bed78",
        "fe3a4914cef5a2031c79d562c3094d800c1955dc55d318917e6210af6ce5fa79",
    }
)


def _texts() -> dict[Path, str]:
    return {path: path.read_bytes().decode("utf-8") for path in sorted(SKILL.rglob("*.md"))}


def _lines() -> set[str]:
    return {line for text in _texts().values() for line in text.splitlines() if line.strip()}


def test_exchange_pitfalls_skill_fits() -> None:
    """Главный файл скилла читается целиком и должен остаться коротким."""
    assert len((SKILL / "SKILL.md").read_bytes()) <= SKILL_MD_LIMIT


def test_exchange_pitfalls_old_lines_kept() -> None:
    """Каждая сохранённая строка прежнего SKILL.md есть в скилле дословно.

    Имя сервера в строке не входит в смысл переноса: хеш считается по прежнему
    написанию, чтобы проверка жила и до переименования проекта, и после.
    """
    previous = "kd2" + "-rules-mcp"
    current_name = "kd" + "-rules-mcp"
    found = {
        hashlib.sha256(line.replace(current_name, previous).encode("utf-8")).hexdigest()
        for line in _lines()
    }
    assert found >= OLD_LINE_HASHES


def test_exchange_pitfalls_changed_links_listed() -> None:
    """Поправленные внутренние ссылки названы и в старой формулировке не остались."""
    assert CHANGED_INTERNAL_LINKS
    assert all(reason.strip() for reason in CHANGED_INTERNAL_LINKS.values())
    current = _lines()
    stale = [line for line in CHANGED_INTERNAL_LINKS if line in current]
    assert stale == []


def test_exchange_pitfalls_route_lists_references() -> None:
    """Каждый справочник скилла назван в таблице маршрута SKILL.md."""
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    start = text.index("## Куда читать\n")
    rest = text[start:]
    end = rest.find("\n## ", 1)
    table = rest if end < 0 else rest[:end]
    present = {path.name for path in (SKILL / "references").glob("*.md")}
    linked = set(re.findall(r"`references/([^`]+\.md)`", table))
    assert linked == present


def test_exchange_pitfalls_references_are_portable() -> None:
    """В справочниках нет путей этого репозитория — как у текста SKILL.md."""
    files = {
        f".claude/skills/kd2-exchange-pitfalls/references/{path.name}": path.read_bytes()
        for path in sorted((SKILL / "references").glob("*.md"))
    }
    assert build_packs.portability_hits(files) == []
