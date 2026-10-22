"""
Outils de mesure et de backtesting de la VaR et de l'Expected Shortfall.

Conventions utilisées partout dans le projet :
- les rendements sont arithmétiques et journaliers ;
- la VaR et l'ES sont exprimées comme des pertes POSITIVES, en euros ;
- toute mesure « du jour t » n'utilise que les données jusqu'à t-1 (shift(1)),
  comme le ferait une banque le matin avant l'ouverture.
"""

import numpy as np
import pandas as pd
from scipy.special import xlogy
from scipy.stats import binom, chi2, norm


# ---------------------------------------------------------------------------
# Rendements
# ---------------------------------------------------------------------------

def rendements_portefeuille(prix: pd.DataFrame, poids: pd.Series) -> pd.Series:
    """Rendement journalier d'un portefeuille rééquilibré chaque jour.

    Les poids sont indexés par ticker : l'ordre des colonnes de `prix` n'a pas d'importance.
    """
    rend = prix.pct_change().dropna()
    return (rend[poids.index] * poids).sum(axis=1).rename("rend_pf")


# ---------------------------------------------------------------------------
# VaR historique
# ---------------------------------------------------------------------------

def var_historique(pnl: pd.Series, alpha: float = 0.01, fenetre: int | None = 250,
                   min_obs: int = 250) -> pd.Series:
    """VaR historique recalculée chaque jour avec les P&L jusqu'à la veille.

    fenetre=250  -> fenêtre glissante des 250 derniers jours
    fenetre=None -> fenêtre croissante (tout l'historique disponible, au moins `min_obs` jours)
    """
    if fenetre is None:
        q = pnl.expanding(min_periods=min_obs).quantile(alpha)
    else:
        q = pnl.rolling(fenetre).quantile(alpha)
    return -q.shift(1)


# ---------------------------------------------------------------------------
# Volatilité EWMA et VaR paramétrique
# ---------------------------------------------------------------------------

def sigma_ewma(rend: pd.Series, lam: float = 0.94) -> pd.Series:
    """Volatilité EWMA (RiskMetrics) connue le matin du jour t.

    sigma²_t = lam * sigma²_{t-1} + (1 - lam) * r²_{t-1}, moyenne des rendements supposée nulle.
    """
    variance = (rend ** 2).ewm(alpha=1 - lam, adjust=False).mean()
    return np.sqrt(variance).shift(1)


def var_ewma_normale(sigma: pd.Series, montant: float, alpha: float = 0.01) -> pd.Series:
    """VaR paramétrique : quantile de la loi normale × volatilité EWMA du jour."""
    return -norm.ppf(alpha) * sigma * montant


# ---------------------------------------------------------------------------
# Simulation historique filtrée (FHS)
# ---------------------------------------------------------------------------

def rendements_standardises(rend: pd.Series, sigma: pd.Series, rodage: int = 30) -> pd.Series:
    """Rendement du jour divisé par la volatilité connue le matin.

    Les `rodage` premiers jours sont écartés, le temps que l'EWMA se stabilise.
    """
    return (rend / sigma).iloc[rodage:]


def moyenne_queue(x: np.ndarray, alpha: float = 0.01) -> float:
    """Moyenne des valeurs situées au-delà du quantile alpha (queue gauche)."""
    seuil = np.quantile(x, alpha)
    return x[x <= seuil].mean()


def var_fhs(z: pd.Series, sigma: pd.Series, montant: float, alpha: float = 0.01,
            min_obs: int = 250) -> pd.Series:
    """VaR FHS : quantile historique des rendements standardisés passés × volatilité du jour."""
    q = z.expanding(min_periods=min_obs).quantile(alpha).shift(1)
    return -q * sigma * montant


def es_fhs(z: pd.Series, sigma: pd.Series, montant: float, alpha: float = 0.01,
           min_obs: int = 250) -> pd.Series:
    """ES FHS : moyenne de la queue des rendements standardisés passés × volatilité du jour."""
    m = z.expanding(min_periods=min_obs).apply(moyenne_queue, raw=True, args=(alpha,)).shift(1)
    return -m * sigma * montant


# ---------------------------------------------------------------------------
# Mesures statiques (sur tout l'échantillon)
# ---------------------------------------------------------------------------

def var_es_historiques(pnl: pd.Series, alpha: float) -> tuple[float, float, int]:
    """VaR et ES historiques sur tout l'échantillon, et nombre de jours dans la queue."""
    seuil = pnl.quantile(alpha)
    queue = pnl[pnl <= seuil]
    return -seuil, -queue.mean(), len(queue)


def var_es_normales(sigma: float, alpha: float) -> tuple[float, float]:
    """VaR et ES d'une loi normale centrée d'écart-type sigma (formules fermées)."""
    z = norm.ppf(alpha)
    return -z * sigma, sigma * norm.pdf(z) / alpha


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------

def intervalle_binomial(T: int, p: float = 0.01, niveau: float = 0.95) -> tuple[float, float]:
    """Nombre de dépassements entre lesquels tombent `niveau` des modèles parfaits."""
    a = (1 - niveau) / 2
    bas, haut = binom.ppf([a, 1 - a], T, p)
    return bas, haut


def kupiec(n_dep: int, T: int, p: float = 0.01) -> tuple[float, float]:
    """Test de Kupiec (1995) : le taux de dépassement est-il compatible avec p ?

    Renvoie la statistique LR (χ² à 1 degré de liberté sous H0) et la p-value.
    """
    pi = n_dep / T
    ll_h0 = xlogy(T - n_dep, 1 - p) + xlogy(n_dep, p)
    ll_obs = xlogy(T - n_dep, 1 - pi) + xlogy(n_dep, pi)
    LR = -2 * (ll_h0 - ll_obs)
    return LR, chi2.sf(LR, df=1)


def christoffersen(dep: pd.Series) -> dict:
    """Test d'indépendance de Christoffersen (1998) sur une série de dépassements (True/False).

    Compare P(dépassement | pas hier) et P(dépassement | dépassement hier).
    """
    d = dep.astype(int).values
    hier, auj = d[:-1], d[1:]

    n00 = int(((hier == 0) & (auj == 0)).sum())
    n01 = int(((hier == 0) & (auj == 1)).sum())
    n10 = int(((hier == 1) & (auj == 0)).sum())
    n11 = int(((hier == 1) & (auj == 1)).sum())

    pi01 = n01 / (n00 + n01)
    pi11 = n11 / (n10 + n11) if (n10 + n11) > 0 else 0.0
    pi = (n01 + n11) / (n00 + n01 + n10 + n11)

    ll_h0 = xlogy(n00 + n10, 1 - pi) + xlogy(n01 + n11, pi)
    ll_h1 = (xlogy(n00, 1 - pi01) + xlogy(n01, pi01)
             + xlogy(n10, 1 - pi11) + xlogy(n11, pi11))
    LR = -2 * (ll_h0 - ll_h1)
    return {"n11": n11, "pi01": pi01, "pi11": pi11, "LR_ind": LR, "p_ind": chi2.sf(LR, df=1)}


def tableau_backtest(pnl: pd.Series, var: pd.DataFrame, p: float = 0.01) -> pd.DataFrame:
    """Kupiec, Christoffersen et couverture conditionnelle pour chaque colonne de VaR.

    `pnl` et `var` doivent couvrir exactement la même période.
    """
    T = len(pnl)
    lignes = {}
    for nom in var.columns:
        dep = pnl < -var[nom]
        LR_pof, p_pof = kupiec(int(dep.sum()), T, p)
        ind = christoffersen(dep)
        lignes[nom] = {
            "dépassements": int(dep.sum()),
            "taux": dep.mean(),
            "LR_Kupiec": LR_pof,
            "p_Kupiec": p_pof,
            "π01": ind["pi01"],
            "π11": ind["pi11"],
            "LR_indép": ind["LR_ind"],
            "p_indép": ind["p_ind"],
            "p_couverture_cond": chi2.sf(LR_pof + ind["LR_ind"], df=2),
        }
    return pd.DataFrame(lignes).T
