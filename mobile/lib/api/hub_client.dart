/// Cliente HTTP del hub.
///
/// El hub no tiene interfaz web: solo JSON y SSE. Se accede por la red de
/// Tailscale, así que la autenticación es un token simple en la cabecera.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models.dart';

class HubException implements Exception {
  HubException(this.mensaje, {this.codigo});

  final String mensaje;
  final int? codigo;

  @override
  String toString() => codigo == null ? mensaje : '$mensaje (HTTP $codigo)';
}

class HubClient {
  HubClient({required this.baseUrl, required this.token, http.Client? cliente})
      : _cliente = cliente ?? http.Client();

  final String baseUrl;
  final String token;
  final http.Client _cliente;

  Map<String, String> get _cabeceras => {
        if (token.isNotEmpty) 'Authorization': 'Bearer $token',
        'Content-Type': 'application/json',
      };

  Uri _uri(String ruta, [Map<String, dynamic>? params]) {
    final base = Uri.parse(baseUrl.endsWith('/')
        ? baseUrl.substring(0, baseUrl.length - 1)
        : baseUrl);
    return base.replace(
      path: '${base.path}$ruta',
      queryParameters: params?.map((k, v) => MapEntry(k, '$v')),
    );
  }

  Future<dynamic> _get(String ruta, [Map<String, dynamic>? params]) async {
    final respuesta = await _cliente
        .get(_uri(ruta, params), headers: _cabeceras)
        .timeout(const Duration(seconds: 30));
    return _decodificar(respuesta);
  }

  Future<dynamic> _post(String ruta, Object cuerpo) async {
    final respuesta = await _cliente
        .post(_uri(ruta), headers: _cabeceras, body: jsonEncode(cuerpo))
        .timeout(const Duration(seconds: 30));
    return _decodificar(respuesta);
  }

  dynamic _decodificar(http.Response r) {
    if (r.statusCode == 401 || r.statusCode == 403) {
      throw HubException('El token no es válido', codigo: r.statusCode);
    }
    if (r.statusCode >= 400) {
      throw HubException('El hub respondió con error', codigo: r.statusCode);
    }
    if (r.body.isEmpty) return null;
    return jsonDecode(utf8.decode(r.bodyBytes));
  }

  // ------------------------------------------------------------ diagnóstico
  Future<Map<String, dynamic>> health() async =>
      (await _get('/health')) as Map<String, dynamic>;

  // ---------------------------------------------------------- sincronización
  Future<void> register(String deviceId, String nombre, SyncScope scope) async {
    await _post('/sync/register', {
      'device_id': deviceId,
      'name': nombre,
      'scope': scope.toJson(),
    });
  }

  Future<Map<String, dynamic>> snapshot(String deviceId, {int? days}) async {
    final params = <String, dynamic>{'device_id': deviceId};
    if (days != null) params['days'] = days;
    return (await _get('/sync/snapshot', params)) as Map<String, dynamic>;
  }

  Future<({List<ChangeOp> ops, int cursor, bool hasMore})> pull(
    String deviceId,
    int since, {
    int limit = 2000,
  }) async {
    final json = (await _get('/sync/pull', {
      'since': since,
      'limit': limit,
      'device_id': deviceId,
    })) as Map<String, dynamic>;

    final ops = (json['ops'] as List)
        .map((o) => ChangeOp.fromJson(o as Map<String, dynamic>))
        .toList();
    return (
      ops: ops,
      cursor: (json['cursor'] ?? since) as int,
      hasMore: json['has_more'] == true,
    );
  }

  Future<({int accepted, int rejected})> push(
      String deviceId, List<ChangeOp> ops) async {
    final json = (await _post('/sync/push', {
      'device_id': deviceId,
      'ops': ops.map((o) => o.toJson()).toList(),
    })) as Map<String, dynamic>;
    return (
      accepted: (json['accepted'] ?? 0) as int,
      rejected: (json['rejected'] ?? 0) as int,
    );
  }

  // ----------------------------------------------------------------- lectura
  /// El cuerpo del artículo no viaja en el snapshot: se pide al abrirlo y se
  /// guarda en local para poder releerlo sin conexión.
  Future<Entry> entrada(String id) async =>
      Entry.fromJson((await _get('/entries/$id')) as Map<String, dynamic>);

  Future<List<Entry>> entradas({int limit = 100, bool soloSinLeer = false}) async {
    final json = (await _get('/entries', {
      'limit': limit,
      if (soloSinLeer) 'unread': true,
    })) as List;
    return json.map((e) => Entry.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> refrescar() async => _post('/feeds/refresh', const {});

  void close() => _cliente.close();
}
