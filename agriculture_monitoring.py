"""
Application Streamlit de Surveillance Agricole
Monitoring des conditions d'un champ avec données Open-Meteo.
Contrôle de la vanne (ESP32/Heltec) via Firebase Realtime Database.
"""

import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta
import time

# Firebase : même base que l'Arduino (path /vanne/etat)
FIREBASE_DATABASE_URL = os.environ.get(
    "FIREBASE_DATABASE_URL",
    "https://esp32-spi-projet-default-rtdb.europe-west1.firebasedatabase.app"
)
# Priorité : fichier local dans le dossier de l'app, puis GOOGLE_APPLICATION_CREDENTIALS
_DIR_APP = os.path.dirname(os.path.abspath(__file__))
FIREBASE_CREDENTIALS_PATH = os.path.join(_DIR_APP, "firebase_credentials.json")
if not os.path.isfile(FIREBASE_CREDENTIALS_PATH) and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    FIREBASE_CREDENTIALS_PATH = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]

# Cache pour l'instance Firebase (éviter ré-init à chaque rerun)
_firebase_app = None
_firebase_error = None  # Dernière erreur pour affichage diagnostic


def get_firebase_app():
    """Initialise et retourne l'app Firebase si les credentials sont présents."""
    global _firebase_app, _firebase_error
    if _firebase_app is not None:
        return _firebase_app
    _firebase_error = None
    if not os.path.isfile(FIREBASE_CREDENTIALS_PATH):
        _firebase_error = f"Fichier introuvable : {FIREBASE_CREDENTIALS_PATH}"
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials
        # Si déjà initialisé (ex: re-run Streamlit), récupérer l'app existante
        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        _firebase_app = firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DATABASE_URL})
        return _firebase_app
    except Exception as e:
        _firebase_error = str(e)
        return None


def firebase_get_vanne_etat():
    """Lit l'état actuel de la vanne depuis Firebase (/vanne/etat). Retourne None si indisponible."""
    try:
        app = get_firebase_app()
        if app is None:
            return None
        from firebase_admin import db
        ref = db.reference("/vanne/etat")
        return ref.get()
    except Exception:
        return None


def firebase_set_vanne_etat(etat: bool) -> bool:
    """Écrit l'état de la vanne dans Firebase (/vanne/etat). Retourne True si succès."""
    try:
        app = get_firebase_app()
        if app is None:
            return False
        from firebase_admin import db
        ref = db.reference("/vanne")
        ref.update({"etat": etat})
        return True
    except Exception:
        return False

# Configuration de la page
st.set_page_config(
    page_title="Surveillance Agricole - Monitoring Champ",
    page_icon="🌾",
    layout="wide"
)

# Titre principal
st.title("🌾 Surveillance des Conditions Agricoles")
st.markdown("### Monitoring de l'humidité de l'air et du sol en temps réel")

# ============================================================================
# FONCTIONS UTILITAIRES (définies en premier)
# ============================================================================

def search_address_suggestions(query, limit=10):
    """
    Recherche des suggestions d'adresses pour l'autocomplétion.
    Utilise l'API Nominatim d'OpenStreetMap (gratuite, sans clé API).
    
    Paramètres:
    - query : chaîne de recherche (adresse partielle)
    - limit : nombre maximum de suggestions à retourner
    
    Retourne:
    - liste de dict avec 'display_name', 'lat', 'lon' ou liste vide en cas d'erreur
    """
    if not query or len(query) < 2:
        return []
    
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": query,
            "format": "json",
            "limit": limit,
            "addressdetails": 1
        }
        headers = {
            "User-Agent": "Agriculture-Monitoring-App"  # Requis par Nominatim
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        suggestions = []
        if data and len(data) > 0:
            for result in data:
                suggestions.append({
                    "display_name": result.get("display_name", ""),
                    "lat": float(result["lat"]),
                    "lon": float(result["lon"])
                })
        
        return suggestions
            
    except requests.exceptions.Timeout:
        return []
    except requests.exceptions.RequestException:
        return []
    except (KeyError, ValueError):
        return []
    except Exception:
        return []


def geocode_address(address):
    """
    Convertit une adresse en coordonnées géographiques (géocodage).
    Utilise l'API Nominatim d'OpenStreetMap (gratuite, sans clé API).
    
    Paramètres:
    - address : chaîne contenant l'adresse ou le nom du lieu
    
    Retourne:
    - dict avec 'lat', 'lon', et 'display_name' ou None en cas d'erreur
    """
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": address,
            "format": "json",
            "limit": 1,
            "addressdetails": 1
        }
        headers = {
            "User-Agent": "Agriculture-Monitoring-App"  # Requis par Nominatim
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if data and len(data) > 0:
            result = data[0]
            return {
                "lat": float(result["lat"]),
                "lon": float(result["lon"]),
                "display_name": result.get("display_name", address)
            }
        else:
            return None
            
    except requests.exceptions.Timeout:
        st.sidebar.error("⏱️ Timeout lors de la recherche d'adresse")
        return None
    except requests.exceptions.RequestException as e:
        st.sidebar.error(f"❌ Erreur de connexion : {str(e)}")
        return None
    except (KeyError, ValueError) as e:
        st.sidebar.error(f"❌ Format de réponse inattendu")
        return None
    except Exception as e:
        st.sidebar.error(f"❌ Erreur inattendue : {str(e)}")
        return None

# ============================================================================
# SIDEBAR - Configuration
# ============================================================================
st.sidebar.header("⚙️ Configuration")

# Coordonnées par défaut : Zone agricole en France (région Centre)
# Vous pouvez modifier ces valeurs pour cibler d'autres zones agricoles
DEFAULT_LATITUDE = 47.5  # Latitude d'une zone agricole en France (région Centre)
DEFAULT_LONGITUDE = 2.0  # Longitude correspondante

# Initialisation des coordonnées dans la session state
if "latitude" not in st.session_state:
    st.session_state.latitude = DEFAULT_LATITUDE
if "longitude" not in st.session_state:
    st.session_state.longitude = DEFAULT_LONGITUDE

# Option de recherche par adresse
st.sidebar.subheader("📍 Recherche par localisation")
search_method = st.sidebar.radio(
    "Méthode de recherche",
    ["Adresse / Zone", "Coordonnées GPS"],
    help="Choisissez de rechercher par adresse ou directement par coordonnées"
)

if search_method == "Adresse / Zone":
    # Initialisation de la session state pour les suggestions
    if "address_query" not in st.session_state:
        st.session_state.address_query = ""
    if "address_suggestions" not in st.session_state:
        st.session_state.address_suggestions = []
    if "selected_address_index" not in st.session_state:
        st.session_state.selected_address_index = None
    
    # Champ de recherche avec autocomplétion
    address_input = st.sidebar.text_input(
        "Entrez une adresse ou un lieu",
        value=st.session_state.address_query,
        placeholder="Ex: Paris, France ou 123 Rue de la Ferme, Orléans",
        help="Tapez au moins 2 caractères pour voir les suggestions",
        key="address_search_input"
    )
    
    # Recherche de suggestions en temps réel (si au moins 2 caractères)
    if address_input and len(address_input) >= 2:
        # Recherche des suggestions (avec un petit délai pour éviter trop de requêtes)
        if address_input != st.session_state.address_query:
            with st.spinner("🔍 Recherche de suggestions..."):
                suggestions = search_address_suggestions(address_input, limit=10)
                st.session_state.address_suggestions = suggestions
                st.session_state.address_query = address_input
    elif len(address_input) < 2:
        st.session_state.address_suggestions = []
        st.session_state.address_query = address_input
    
    # Affichage des suggestions dans un selectbox
    if st.session_state.address_suggestions:
        suggestion_options = [f"{idx + 1}. {sug['display_name']}" 
                             for idx, sug in enumerate(st.session_state.address_suggestions)]
        suggestion_options.insert(0, "Sélectionnez une adresse dans la liste...")
        
        selected_suggestion = st.sidebar.selectbox(
            "Suggestions d'adresses",
            options=suggestion_options,
            index=0,
            help="Choisissez une adresse dans la liste ou continuez à taper pour affiner la recherche"
        )
        
        # Si une suggestion est sélectionnée (pas l'option par défaut)
        if selected_suggestion and selected_suggestion != suggestion_options[0]:
            # Extraire l'index de la suggestion sélectionnée
            try:
                selected_index = suggestion_options.index(selected_suggestion) - 1
                if 0 <= selected_index < len(st.session_state.address_suggestions):
                    selected_address = st.session_state.address_suggestions[selected_index]
                    
                    # Mise à jour automatique des coordonnées
                    st.session_state.latitude = selected_address["lat"]
                    st.session_state.longitude = selected_address["lon"]
                    st.sidebar.success(f"✅ Localisation sélectionnée : {selected_address['display_name'][:60]}...")
            except (ValueError, IndexError):
                pass
    
    # Bouton de recherche manuelle (si l'utilisateur veut forcer la recherche)
    if st.sidebar.button("🔍 Rechercher cette adresse", type="primary"):
        if address_input:
            with st.spinner("Recherche de la localisation..."):
                coords = geocode_address(address_input)
                if coords:
                    st.session_state.latitude = coords["lat"]
                    st.session_state.longitude = coords["lon"]
                    st.sidebar.success(f"✅ Localisation trouvée : {coords.get('display_name', '')[:50]}...")
                else:
                    st.sidebar.error("❌ Adresse non trouvée. Vérifiez l'orthographe.")
    
    # Affichage des coordonnées trouvées
    st.sidebar.caption(f"📍 Coordonnées actuelles : {st.session_state.latitude:.4f}°N, {st.session_state.longitude:.4f}°E")
    latitude = st.session_state.latitude
    longitude = st.session_state.longitude
else:
    # Mode coordonnées GPS directes
    latitude = st.sidebar.number_input(
        "Latitude",
        min_value=-90.0,
        max_value=90.0,
        value=st.session_state.latitude,
        step=0.1,
        format="%.4f",
        help="Coordonnée latitude du champ à surveiller",
        key="lat_input"
    )
    
    longitude = st.sidebar.number_input(
        "Longitude",
        min_value=-180.0,
        max_value=180.0,
        value=st.session_state.longitude,
        step=0.1,
        format="%.4f",
        help="Coordonnée longitude du champ à surveiller",
        key="lon_input"
    )
    
    # Mise à jour de la session state
    st.session_state.latitude = latitude
    st.session_state.longitude = longitude

# Sélecteur de dates pour l'historique
st.sidebar.subheader("📅 Période d'historique")
date_end = st.sidebar.date_input(
    "Date de fin",
    value=datetime.now().date(),
    max_value=datetime.now().date()
)

date_start = st.sidebar.date_input(
    "Date de début",
    value=(datetime.now() - timedelta(days=7)).date(),
    max_value=date_end,
    help="Par défaut : 7 derniers jours"
)

# Validation : date de début doit être antérieure à date de fin
if date_start >= date_end:
    st.sidebar.warning("⚠️ La date de début doit être antérieure à la date de fin")
    date_start = date_end - timedelta(days=1)

# ============================================================================
# FONCTIONS UTILITAIRES (suite)
# ============================================================================

def fetch_open_meteo_data(lat, lon, start_date, end_date):
    """
    Récupère les données météorologiques depuis l'API Open-Meteo.
    
    Paramètres:
    - lat, lon : coordonnées géographiques
    - start_date, end_date : dates de début et fin (format date)
    
    Retourne:
    - dict avec les données ou None en cas d'erreur
    
    Note pour changer la profondeur du sol :
    - Actuellement : soil_moisture_0_to_1cm (0-1 cm)
    - Pour 3-9 cm : remplacer par soil_moisture_3_to_9cm
    - Pour 9-27 cm : remplacer par soil_moisture_9_to_27cm
    - Autres profondeurs disponibles : 27-81cm, 81-243cm
    - Voir documentation : https://open-meteo.com/en/docs
    """
    try:
        # Détermination de l'endpoint selon la période
        # L'endpoint "forecast" fonctionne pour les données récentes (jusqu'à ~7 jours)
        # Pour des données plus anciennes, utiliser "archive" (si disponible)
        today = datetime.now().date()
        days_diff = (today - start_date).days
        
        # Utilisation de l'endpoint forecast (fonctionne pour données récentes)
        url = "https://api.open-meteo.com/v1/forecast"
        
        # Paramètres de l'API
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "relative_humidity_2m,soil_moisture_0_to_1cm",
            # Pour changer la profondeur du sol, modifier le paramètre ci-dessus :
            # Exemple pour 3-9cm : "hourly": "relative_humidity_2m,soil_moisture_3_to_9cm"
            # Exemple pour 9-27cm : "hourly": "relative_humidity_2m,soil_moisture_9_to_27cm"
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "timezone": "Europe/Paris"
        }
        
        # Appel API avec timeout
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()  # Lève une exception si erreur HTTP
        
        data = response.json()
        
        # Vérification de la structure des données
        if "hourly" not in data:
            st.error("❌ Format de données inattendu de l'API")
            return None
        
        # Vérification que les données contiennent bien les paramètres demandés
        hourly = data.get("hourly", {})
        if not hourly.get("relative_humidity_2m") or not hourly.get("soil_moisture_0_to_1cm"):
            st.warning("⚠️ Certains paramètres ne sont pas disponibles pour cette localisation")
            return None
            
        return data
        
    except requests.exceptions.Timeout:
        st.error("⏱️ Timeout : L'API Open-Meteo ne répond pas. Veuillez réessayer.")
        return None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400:
            st.error("❌ Requête invalide. Vérifiez les coordonnées et les dates.")
        else:
            st.error(f"❌ Erreur HTTP {e.response.status_code} : {str(e)}")
        return None
    except requests.exceptions.RequestException as e:
        st.error(f"❌ Erreur de connexion à l'API : {str(e)}")
        return None
    except Exception as e:
        st.error(f"❌ Erreur inattendue : {str(e)}")
        return None


def process_meteo_data(api_data):
    """
    Traite les données de l'API et les convertit en DataFrame pandas.
    
    Retourne:
    - DataFrame avec colonnes : datetime, humidity_air, humidity_soil
    - None en cas d'erreur
    """
    try:
        if api_data is None:
            return None
            
        hourly_data = api_data.get("hourly", {})
        times = hourly_data.get("time", [])
        humidity_air = hourly_data.get("relative_humidity_2m", [])
        humidity_soil = hourly_data.get("soil_moisture_0_to_1cm", [])
        
        # Vérification que les données existent
        if not times or not humidity_air or not humidity_soil:
            st.warning("⚠️ Données incomplètes reçues de l'API")
            return None
        
        # Création du DataFrame
        df = pd.DataFrame({
            "datetime": pd.to_datetime(times),
            "humidity_air": humidity_air,
            "humidity_soil": humidity_soil
        })
        
        # Suppression des valeurs nulles
        df = df.dropna()
        
        if df.empty:
            st.warning("⚠️ Aucune donnée valide après traitement")
            return None
            
        return df
        
    except Exception as e:
        st.error(f"❌ Erreur lors du traitement des données : {str(e)}")
        return None


# ============================================================================
# RÉCUPÉRATION DES DONNÉES
# ============================================================================

# Bouton pour actualiser les données
if st.sidebar.button("🔄 Actualiser les données", type="primary"):
    st.rerun()

# Affichage d'un spinner pendant le chargement
with st.spinner("🔄 Chargement des données depuis Open-Meteo..."):
    api_data = fetch_open_meteo_data(latitude, longitude, date_start, date_end)
    df = process_meteo_data(api_data)

# ============================================================================
# CONTRÔLE VANNE (Firebase / ESP32)
# ============================================================================
st.subheader("🚰 Contrôle de la vanne (ESP32 / Heltec)")
st.caption("Commande envoyée à Firebase Realtime Database (path : /vanne/etat). Votre Arduino lit cette valeur et pilote la LED/vanne.")

firebase_ok = get_firebase_app() is not None
if not firebase_ok:
    err_msg = _firebase_error or "Fichier firebase_credentials.json introuvable."
    st.warning(
        "⚠️ **Firebase non configuré** — Pour piloter la vanne depuis le site, ajoutez le fichier de compte de service Firebase : "
        "téléchargez-le depuis la console Firebase (Paramètres du projet → Comptes de service → Générer une nouvelle clé privée) "
        "et enregistrez-le sous le nom `firebase_credentials.json` dans le dossier de l'application."
    )
    st.error(f"**Détail :** {err_msg}")
else:
    # Lecture de l'état actuel depuis Firebase (même path que l'Arduino : /vanne/etat)
    etat_actuel = firebase_get_vanne_etat()
    if etat_actuel is None:
        etat_actuel = False  # défaut : éteint
    if "vanne_etat" not in st.session_state:
        st.session_state.vanne_etat = etat_actuel
    # Synchroniser l'affichage avec Firebase à chaque chargement
    st.session_state.vanne_etat = etat_actuel

    col_vanne1, col_vanne2 = st.columns([1, 2])
    with col_vanne1:
        nouveau_etat = st.toggle("Vanne **ON** / OFF", value=st.session_state.vanne_etat, key="vanne_toggle")
    with col_vanne2:
        if nouveau_etat != etat_actuel:
            if firebase_set_vanne_etat(nouveau_etat):
                st.session_state.vanne_etat = nouveau_etat
                st.success("État envoyé à Firebase : **" + ("ON" if nouveau_etat else "OFF") + "** — l'ESP32 va mettre à jour la vanne/LED.")
            else:
                st.error("Impossible d'écrire dans Firebase.")
        else:
            st.info("État actuel : **" + ("ON" if etat_actuel else "OFF") + "** (synchronisé avec l'ESP32)")

st.markdown("---")

# ============================================================================
# DASHBOARD PRINCIPAL
# ============================================================================

if df is not None and not df.empty:
    # --- CARTE ---
    st.subheader("📍 Localisation du champ")
    
    # Création d'un DataFrame pour la carte (st.map nécessite lat/lon)
    map_data = pd.DataFrame({
        "lat": [latitude],
        "lon": [longitude]
    })
    
    # Affichage de la carte avec un point rouge
    st.map(map_data, zoom=10)
    st.caption(f"Coordonnées : {latitude:.4f}°N, {longitude:.4f}°E")
    
    # --- KPIs (Indicateurs actuels) ---
    st.subheader("📊 Indicateurs actuels")
    
    # Récupération des valeurs les plus récentes
    latest_data = df.iloc[-1]
    current_air_humidity = latest_data["humidity_air"]
    current_soil_humidity = latest_data["humidity_soil"]
    last_update = latest_data["datetime"]
    
    # Affichage des KPIs en colonnes
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric(
            label="💨 Humidité de l'Air",
            value=f"{current_air_humidity:.1f}%",
            help="Humidité relative à 2 mètres du sol"
        )
    
    with col2:
        st.metric(
            label="🌱 Humidité du Sol",
            value=f"{current_soil_humidity:.3f} m³/m³",
            help="Humidité du sol en surface (0-1 cm)"
        )
    
    with col3:
        st.metric(
            label="🕐 Dernière mise à jour",
            value=last_update.strftime("%H:%M"),
            help=f"Date : {last_update.strftime('%d/%m/%Y')}"
        )
    
    # --- GRAPHIQUES D'HISTORIQUE ---
    st.subheader("📈 Évolution temporelle")
    
    # Création du graphique avec Plotly (deux courbes sur le même graphique)
    fig = go.Figure()
    
    # Courbe pour l'humidité de l'air
    fig.add_trace(go.Scatter(
        x=df["datetime"],
        y=df["humidity_air"],
        mode="lines",
        name="Humidité de l'Air (%)",
        line=dict(color="#1f77b4", width=2),
        hovertemplate="<b>%{fullData.name}</b><br>" +
                      "Date: %{x}<br>" +
                      "Valeur: %{y:.1f}%<extra></extra>"
    ))
    
    # Courbe pour l'humidité du sol
    # Utilisation d'un axe Y secondaire pour mieux visualiser les deux métriques
    fig.add_trace(go.Scatter(
        x=df["datetime"],
        y=df["humidity_soil"],
        mode="lines",
        name="Humidité du Sol (m³/m³)",
        line=dict(color="#ff7f0e", width=2),
        yaxis="y2",
        hovertemplate="<b>%{fullData.name}</b><br>" +
                      "Date: %{x}<br>" +
                      "Valeur: %{y:.3f} m³/m³<extra></extra>"
    ))
    
    # Configuration du layout
    # Note : Dans les nouvelles versions de Plotly, titlefont est remplacé par title.font
    fig.update_layout(
        title="Évolution de l'humidité de l'air et du sol",
        xaxis_title="Date et Heure",
        yaxis=dict(
            title=dict(text="Humidité de l'Air (%)", font=dict(color="#1f77b4")),
            tickfont=dict(color="#1f77b4"),
            side="left"
        ),
        yaxis2=dict(
            title=dict(text="Humidité du Sol (m³/m³)", font=dict(color="#ff7f0e")),
            tickfont=dict(color="#ff7f0e"),
            overlaying="y",
            side="right"
        ),
        hovermode="x unified",
        height=500,
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="left",
            x=0.01
        ),
        template="plotly_white"
    )
    
    st.plotly_chart(fig, use_container_width=True)
    
    # --- TABLEAU DE DONNÉES (optionnel) ---
    with st.expander("📋 Voir les données brutes"):
        st.dataframe(
            df.style.format({
                "humidity_air": "{:.1f}%",
                "humidity_soil": "{:.3f} m³/m³"
            }),
            use_container_width=True
        )
        
        # Bouton de téléchargement
        csv = df.to_csv(index=False)
        st.download_button(
            label="💾 Télécharger les données (CSV)",
            data=csv,
            file_name=f"donnees_agricoles_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv"
        )
    
    # --- STATISTIQUES RÉSUMÉES ---
    st.subheader("📊 Statistiques sur la période")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**Humidité de l'Air**")
        stats_air = df["humidity_air"].describe()
        st.write(f"- **Moyenne** : {stats_air['mean']:.1f}%")
        st.write(f"- **Minimum** : {stats_air['min']:.1f}%")
        st.write(f"- **Maximum** : {stats_air['max']:.1f}%")
        st.write(f"- **Écart-type** : {stats_air['std']:.1f}%")
    
    with col2:
        st.markdown("**Humidité du Sol**")
        stats_soil = df["humidity_soil"].describe()
        st.write(f"- **Moyenne** : {stats_soil['mean']:.3f} m³/m³")
        st.write(f"- **Minimum** : {stats_soil['min']:.3f} m³/m³")
        st.write(f"- **Maximum** : {stats_soil['max']:.3f} m³/m³")
        st.write(f"- **Écart-type** : {stats_soil['std']:.3f} m³/m³")
    
else:
    # Message d'erreur si pas de données
    st.error("❌ Impossible de charger les données. Veuillez vérifier :")
    st.markdown("""
    - Votre connexion internet
    - Les coordonnées géographiques (doivent être valides)
    - La période sélectionnée (les données historiques peuvent être limitées)
    - Que l'API Open-Meteo est accessible
    """)
    
    # Afficher quand même la carte avec les coordonnées
    st.subheader("📍 Localisation du champ")
    map_data = pd.DataFrame({
        "lat": [latitude],
        "lon": [longitude]
    })
    st.map(map_data, zoom=10)

# ============================================================================
# FOOTER / INFORMATIONS
# ============================================================================
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: gray;'>
    <small>
    🌾 Application de Surveillance Agricole | 
    Données fournies par <a href="https://open-meteo.com" target="_blank">Open-Meteo</a> | 
    Développé avec Streamlit
    </small>
</div>
""", unsafe_allow_html=True)

# Instructions pour changer la profondeur du sol (dans la sidebar)
st.sidebar.markdown("---")
st.sidebar.markdown("### 💡 Note technique")
st.sidebar.info("""
**Pour changer la profondeur du sol :**

Modifiez le paramètre `hourly` dans la fonction `fetch_open_meteo_data()` :

- **0-1 cm** : `soil_moisture_0_to_1cm` (actuel)
- **3-9 cm** : `soil_moisture_3_to_9cm`
- **9-27 cm** : `soil_moisture_9_to_27cm`
- **27-81 cm** : `soil_moisture_27_to_81cm`
- **81-243 cm** : `soil_moisture_81_to_243cm`

Voir la documentation : https://open-meteo.com/en/docs
""")

st.sidebar.markdown("### 🚰 Contrôle vanne (Firebase)")
st.sidebar.info("""
Le site écrit l'état de la vanne dans Firebase au path **/vanne/etat** (booléen), comme votre code Arduino.

Pour activer le contrôle :
1. Console Firebase → Paramètres du projet → Comptes de service
2. « Générer une nouvelle clé privée »
3. Enregistrer le fichier sous le nom **firebase_credentials.json** dans le dossier `SPI_Agri`
""")
