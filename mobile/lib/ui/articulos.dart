/// Lista de artículos de un origen (feed, carpeta o vista).
library;

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../app_state.dart';
import '../models.dart';
import 'articulo.dart';

class ArticulosPage extends StatefulWidget {
  const ArticulosPage({
    super.key,
    required this.estado,
    required this.titulo,
    this.feedId,
    this.folderId,
    this.soloSinLeer = false,
    this.soloGuardados = false,
  });

  final AppState estado;
  final String titulo;
  final String? feedId;
  final String? folderId;
  final bool soloSinLeer;
  final bool soloGuardados;

  @override
  State<ArticulosPage> createState() => _ArticulosPageState();
}

class _ArticulosPageState extends State<ArticulosPage> {
  static const _pagina = 50;

  final _scroll = ScrollController();
  final List<Entry> _entradas = [];
  Map<String, String> _titulosFeed = {};
  bool _cargando = false;
  bool _agotado = false;
  String _busqueda = '';

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      // Paginación: la ventana del móvil puede tener miles de artículos y
      // construirlos todos de golpe bloquearía la interfaz.
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 600) {
        _cargarMas();
      }
    });
    _recargar();
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _recargar() async {
    final feeds = await widget.estado.repo.feeds();
    setState(() {
      _titulosFeed = {for (final f in feeds) f.id: f.displayTitle};
      _entradas.clear();
      _agotado = false;
    });
    await _cargarMas();
  }

  Future<void> _cargarMas() async {
    if (_cargando || _agotado) return;
    setState(() => _cargando = true);

    final lote = await widget.estado.repo.entradas(
      feedId: widget.feedId,
      folderId: widget.folderId,
      soloSinLeer: widget.soloSinLeer,
      soloGuardados: widget.soloGuardados,
      busqueda: _busqueda.isEmpty ? null : _busqueda,
      limit: _pagina,
      offset: _entradas.length,
    );

    if (!mounted) return;
    setState(() {
      _entradas.addAll(lote);
      _agotado = lote.length < _pagina;
      _cargando = false;
    });
  }

  Future<void> _abrir(Entry entrada) async {
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ArticuloPage(
        estado: widget.estado,
        entrada: entrada,
        feedTitulo: _titulosFeed[entrada.feedId] ?? '',
      ),
    ));
    await _recargar();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.titulo),
        actions: [
          if (widget.feedId != null)
            IconButton(
              tooltip: 'Marcar todo como leído',
              icon: const Icon(Icons.done_all),
              onPressed: () async {
                await widget.estado.marcarFeedLeido(widget.feedId!);
                await _recargar();
              },
            ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(56),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
            child: TextField(
              decoration: const InputDecoration(
                hintText: 'Buscar en lo descargado…',
                prefixIcon: Icon(Icons.search),
                isDense: true,
                border: OutlineInputBorder(),
              ),
              onSubmitted: (v) {
                _busqueda = v;
                _recargar();
              },
            ),
          ),
        ),
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          await widget.estado.sincronizar();
          await _recargar();
        },
        child: _entradas.isEmpty && !_cargando
            ? ListView(
                children: const [
                  SizedBox(height: 120),
                  Center(child: Text('Nada por aquí.')),
                ],
              )
            : ListView.separated(
                controller: _scroll,
                itemCount: _entradas.length + (_agotado ? 0 : 1),
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, i) {
                  if (i >= _entradas.length) {
                    return const Padding(
                      padding: EdgeInsets.all(24),
                      child: Center(child: CircularProgressIndicator()),
                    );
                  }
                  return _fila(_entradas[i]);
                },
              ),
      ),
    );
  }

  Widget _fila(Entry e) {
    final tema = Theme.of(context);
    final fecha = DateFormat('d MMM, HH:mm', 'es').format(e.published);
    return Dismissible(
      key: ValueKey(e.id),
      background: Container(
        color: Colors.blueGrey,
        alignment: Alignment.centerLeft,
        padding: const EdgeInsets.only(left: 20),
        child: Icon(e.read ? Icons.mark_email_unread : Icons.done,
            color: Colors.white),
      ),
      secondaryBackground: Container(
        color: Colors.amber.shade700,
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.only(right: 20),
        child: Icon(e.starred ? Icons.star_border : Icons.star,
            color: Colors.white),
      ),
      confirmDismiss: (direccion) async {
        // Deslizar no elimina nada: alterna leído o guardado y la fila se queda.
        if (direccion == DismissDirection.startToEnd) {
          await widget.estado.marcarLeido([e.id], !e.read);
        } else {
          await widget.estado.marcarGuardado([e.id], !e.starred);
        }
        await _recargar();
        return false;
      },
      child: ListTile(
        title: Text(
          e.title.isEmpty ? '(sin título)' : e.title,
          maxLines: 3,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
              fontWeight: e.read ? FontWeight.normal : FontWeight.w600),
        ),
        subtitle: Text(
          '${_titulosFeed[e.feedId] ?? ''} · $fecha',
          style: tema.textTheme.bodySmall,
        ),
        trailing: e.starred
            ? const Icon(Icons.star, size: 18, color: Colors.amber)
            : null,
        onTap: () => _abrir(e),
      ),
    );
  }
}
