/// Estado compartido de la aplicación.
///
/// No se usa ningún gestor de estado externo: la app es pequeña y todo lo que
/// hay que compartir es la conexión con el hub, la base local y el resultado de
/// la última sincronización.
library;

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api/hub_client.dart';
import 'data/database.dart';
import 'data/repo.dart';
import 'data/sync.dart';
import 'models.dart';

class AppState extends ChangeNotifier {
  AppState._(this._prefs, this._secure, this._hubToken, this.app, this.repo);

  final SharedPreferences _prefs;
  final FlutterSecureStorage _secure;
  String _hubToken;
  final AppDatabase app;
  final Repo repo;

  static Future<AppState> crear() async {
    final prefs = await SharedPreferences.getInstance();
    const secure = FlutterSecureStorage();
    var token = await secure.read(key: 'hub_token') ?? '';
    // Migración desde las versiones que guardaban el token en preferencias.
    if (token.isEmpty) {
      token = prefs.getString('hub_token') ?? '';
      if (token.isNotEmpty) await secure.write(key: 'hub_token', value: token);
      await prefs.remove('hub_token');
    }
    final db = await AppDatabase.open();
    return AppState._(prefs, secure, token, db, Repo(db));
  }

  // ------------------------------------------------------------ ajustes
  String get hubUrl => _prefs.getString('hub_url') ?? '';
  String get hubToken => _hubToken;
  int get ventanaDias => _prefs.getInt('scope_days') ?? 30;
  bool get sincronizacionEnSegundoPlano =>
      _prefs.getBool('background_sync') ?? true;

  bool get configurado => hubUrl.isNotEmpty;

  Future<void> guardarAjustes({
    required String url,
    required String token,
    int? dias,
    bool? segundoPlano,
  }) async {
    await _prefs.setString('hub_url', url.trim());
    _hubToken = token.trim();
    await _secure.write(key: 'hub_token', value: _hubToken);
    if (dias != null) {
      await _prefs.setInt('scope_days', dias);
    }
    if (segundoPlano != null) {
      await _prefs.setBool('background_sync', segundoPlano);
    }
    notifyListeners();
  }

  // ------------------------------------------------- sincronización
  bool sincronizando = false;
  String? ultimoError;
  SyncStats? ultimasEstadisticas;
  int sinLeer = 0;

  HubClient _cliente() => HubClient(baseUrl: hubUrl, token: hubToken);

  SyncEngine _motor(HubClient cliente) => SyncEngine(
        app: app,
        repo: repo,
        client: cliente,
        scope: SyncScope(days: ventanaDias),
      );

  Future<bool> probarConexion(String url, String token) async {
    final cliente = HubClient(baseUrl: url.trim(), token: token.trim());
    try {
      final salud = await cliente.health();
      ultimoError = null;
      return salud['ok'] == true;
    } on HubException catch (e) {
      ultimoError = e.toString();
      return false;
    } catch (e) {
      ultimoError = 'No se pudo conectar: $e';
      return false;
    } finally {
      cliente.close();
    }
  }

  Future<void> sincronizar() async {
    if (sincronizando || !configurado) return;
    sincronizando = true;
    ultimoError = null;
    notifyListeners();

    final cliente = _cliente();
    try {
      ultimasEstadisticas = await _motor(cliente).syncOnce();
    } on HubException catch (e) {
      ultimoError = e.toString();
    } catch (e) {
      // Sin conexión no es un fallo del que haya que alarmar: los cambios
      // locales siguen en la cola y subirán en el próximo intento.
      ultimoError = 'Sin conexión con el hub';
      debugPrint('sincronización: $e');
    } finally {
      cliente.close();
      await refrescarContadores();
      sincronizando = false;
      notifyListeners();
    }
  }

  Future<void> refrescarContadores() async {
    sinLeer = await repo.totalSinLeer();
  }

  /// Pide el cuerpo del artículo al hub y lo guarda para poder releerlo sin red.
  Future<Entry?> cargarArticulo(String id) async {
    final local = await repo.entrada(id);
    if (local != null && local.hasBody) return local;
    if (!configurado) return local;

    final cliente = _cliente();
    try {
      final remoto = await cliente.entrada(id);
      await repo.guardarCuerpo(id, remoto.bodyHtml, remoto.bodyText);
      return await repo.entrada(id);
    } catch (e) {
      debugPrint('no se pudo traer el cuerpo de $id: $e');
      return local;
    } finally {
      cliente.close();
    }
  }

  Future<void> marcarLeido(List<String> ids, bool leido) async {
    await repo.marcarLeido(ids, leido);
    await refrescarContadores();
    notifyListeners();
  }

  Future<void> marcarGuardado(List<String> ids, bool guardado) async {
    await repo.marcarGuardado(ids, guardado);
    notifyListeners();
  }

  Future<void> marcarFeedLeido(String feedId) async {
    await repo.marcarFeedLeido(feedId);
    await refrescarContadores();
    notifyListeners();
  }

  Future<int> pendientesDeSubir() => repo.cambiosPendientes();

  Future<void> suscribirse(String url) async {
    final cliente = _cliente();
    try {
      await cliente.agregarFeed(url);
      await sincronizar();
    } finally {
      cliente.close();
    }
  }

  Future<void> exportarObsidian(String entryId) async {
    final cliente = _cliente();
    try {
      await cliente.exportarObsidian([entryId]);
    } finally {
      cliente.close();
    }
  }

  Future<void> enviarKindle(String entryId) async {
    final cliente = _cliente();
    try {
      await cliente.enviarKindle([entryId]);
    } finally {
      cliente.close();
    }
  }

  Future<void> olvidarTodo() async {
    await app.vaciar();
    await refrescarContadores();
    notifyListeners();
  }
}
